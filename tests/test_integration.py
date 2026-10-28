"""Loopback HTTP + SQLite + separately implemented JavaScript producer checks."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

from clock_state import ClockState
from reference_api import Store, server
from verify_contract import ROOT, canonical, decode, digest


class Integration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = os.environ.get('NODE_BINARY') or shutil.which('node')
        if not cls.node:
            raise RuntimeError('Node.js required: set NODE_BINARY or add node to PATH')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.b = json.loads((ROOT/'contracts/fixtures/batch.json').read_text(encoding='utf-8'))
        configs = json.loads((ROOT/'contracts/fixtures/sensor-configs.json').read_text(encoding='utf-8'))
        self.store = Store(str(Path(self.temp.name)/'contract.db'), configs,
                           'synthetic-test-token', self.b['machine_id'])
        self.http = server(self.store)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)
        self.url = 'http://127.0.0.1:'+str(self.http.server_port)

    def close(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=5)

    def produce(self, b):
        path = Path(self.temp.name)/'request.json'
        path.write_text(json.dumps(b), encoding='utf-8')
        return json.loads(subprocess.check_output(
            [self.node, str(ROOT/'tests/agent_producer.mjs'), str(path)], text=True))

    def post(self, b=None, token='synthetic-test-token', path='/v1/telemetry/batches', sha=None):
        produced = self.produce(b or self.b)
        return self.raw(produced['body'].encode(), token, path, sha or produced['sha256'])

    def raw(self, raw, token, path, sha):
        request = urllib.request.Request(self.url+path, data=raw, headers={
            'Authorization': 'Bearer '+token, 'Content-Type': 'application/json', 'X-Content-SHA256': sha})
        try:
            with urllib.request.urlopen(request, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as r:
            return r.code, json.loads(r.read())

    def test_independent_producer_golden(self):
        """Expected: independent JS/Python canonical bytes and pinned SHA match."""
        vectors = json.loads((ROOT/'contracts/fixtures/hash-vectors.json').read_text())
        for vector in vectors:
            path = Path(self.temp.name)/'vector.json'
            path.write_text(vector['input'], encoding='utf-8')
            actual = json.loads(subprocess.check_output([self.node, str(ROOT/'tests/agent_producer.mjs'), str(path)], text=True))
            self.assertEqual(actual['body'], vector['canonical'])
            self.assertEqual(actual['sha256'], vector['sha256'])
            self.assertEqual(canonical(decode(vector['input'])), vector['canonical'])

    def test_normal_replay_response_and_quality(self):
        """Expected: replay body incl. received_at identical, exactly 1/1/3/1 rows."""
        first = self.post()
        second = self.post()  # First response may have been lost to the real agent.
        self.assertEqual(first[0], 200)
        self.assertEqual(second, first)
        self.assertEqual(self.store.counts(), dict(batches=1, samples=1, measurements=3, reaggregate=1))
        import sqlite3
        with closing(sqlite3.connect(self.store.path)) as db:
            rows = [json.loads(r[0]) for r in db.execute('SELECT body FROM measurements')]
        self.assertEqual(sorted((r['quality'], r['value']) for r in rows),
                         [('missing', None), ('ok', 52.5), ('unsupported', None)])

    def test_changed_id_and_content(self):
        """Expected: same ID changed value -> BATCH_CONFLICT; new ID same sample -> SAMPLE_CONFLICT."""
        self.post()
        self.b['samples'][0]['readings'][0]['value'] = 53
        status, body = self.post()
        self.assertEqual((status, body['error']['code']), (409, 'BATCH_CONFLICT'))
        self.b['batch_id'] = '55555555-5555-4555-8555-555555555555'
        status, body = self.post()
        self.assertEqual((status, body['error']['code']), (409, 'SAMPLE_CONFLICT'))
        self.assertEqual(self.store.counts()['measurements'], 3)

    def test_old_catalog_and_pending_registration(self):
        """Expected: old catalog still accepts; missing catalog retryable 409, same frozen body succeeds after registration."""
        self.assertEqual(self.post()[0], 200)  # Fixture contains a second newer config too.
        self.b['batch_id'] = '55555555-5555-4555-8555-555555555555'
        self.b['samples'][0]['sample_seq'] = 1
        key = self.b['sensor_config_id']
        config = self.store.configs.pop(key)
        status, body = self.post()
        self.assertEqual((status, body['error']['code'], body['error']['retryable']),
                         (409, 'SENSOR_CONFIG_PENDING', True))
        self.store.configs[key] = config
        self.assertEqual(self.post()[0], 200)

    def test_invalid_last_sample_no_partial_success(self):
        """Expected: last sample invalid -> 422; all four tables empty."""
        sample = copy.deepcopy(self.b['samples'][0])
        sample['sample_seq'] = 1
        sample['readings'][0]['value'] = None
        self.b['samples'].append(sample)
        self.assertEqual(self.post()[0], 422)
        self.assertEqual(list(self.store.counts().values()), [0]*4)

    def test_actual_transaction_rollback(self):
        """Expected: injected failure after INSERTs rolls back all tables; identical retry -> 200."""
        self.store.fail_before_commit = True
        self.assertEqual(self.post()[0], 503)
        self.assertEqual(list(self.store.counts().values()), [0]*4)
        self.store.fail_before_commit = False
        self.assertEqual(self.post()[0], 200)

    def test_concurrent_identical_requests(self):
        """Expected: 4 concurrent HTTP posts return the same 200 body and one stored sample."""
        produced = self.produce(self.b)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.raw(produced['body'].encode(),
                'synthetic-test-token', '/v1/telemetry/batches', produced['sha256']), range(4)))
        self.assertEqual([r[0] for r in results], [200]*4)
        self.assertTrue(all(r == results[0] for r in results))
        self.assertEqual(self.store.counts()['samples'], 1)

    def test_auth_hash_and_unsupported_zero(self):
        """Expected: bad token 401, other machine 403, bad hash 422, unsupported zero 422; no writes."""
        self.assertEqual(self.post(token='expired')[0], 401)
        other = copy.deepcopy(self.b)
        other['machine_id'] = '99999999-9999-4999-8999-999999999999'
        self.assertEqual(self.post(other)[0], 403)
        self.assertEqual(self.post(sha='0'*64)[0], 422)
        self.b['samples'][0]['readings'][1]['value'] = 0
        self.assertEqual(self.post()[0], 422)
        self.assertEqual(list(self.store.counts().values()), [0]*4)

    def test_time_http_and_error(self):
        """Expected: request ID echoed, ordered UTC, precision=1000; unsynced -> CLOCK_UNTRUSTED."""
        request = dict(schema_version='0.1.0', request_id='44444444-4444-4444-8444-444444444444')
        status, body = self.post(request, path='/v1/device/time')
        self.assertEqual(status, 200)
        self.assertEqual(body['request_id'], request['request_id'])
        self.assertLessEqual(body['server_received_at'], body['server_sent_at'])
        self.assertEqual(body['precision_us'], 1000)
        self.store.clock_synced = False
        status, body = self.post(request, path='/v1/device/time')
        self.assertEqual((status, body['error']['code'], body['error']['request_id']),
                         (503, 'CLOCK_UNTRUSTED', request['request_id']))

    def test_hash_collision_still_compares_body(self):
        """Expected: artificially equal hashes cannot bypass canonical content comparison -> 409."""
        with patch('reference_api.digest', return_value='a'*64):
            self.assertEqual(self.post(sha='a'*64)[0], 200)
            self.b['samples'][0]['readings'][0]['value'] = 54
            self.assertEqual(self.post(sha='a'*64)[0], 409)

    def test_invalid_types_and_duplicate_json(self):
        """Expected: malformed batch types -> 422; duplicate JSON keys -> 400; no writes."""
        for path in ('batch_id', 'machine_id', 'samples'):
            bad = copy.deepcopy(self.b)
            bad[path] = []
            self.assertEqual(self.post(bad)[0], 422)
        self.assertEqual(self.raw(b'{"x":1,"x":2}', 'synthetic-test-token',
            '/v1/telemetry/batches', '0'*64)[0], 400)
        self.assertEqual(list(self.store.counts().values()), [0]*4)


class StateTransitions(unittest.TestCase):
    def accept(self, state, rid, offset=6000):
        state.request(rid, 0, 0)
        return state.response(rid, offset+50, offset+60, 110, 110)

    def test_three_consecutive_threshold_and_recovery(self):
        """Expected: 3 high offsets -> suspected; normal sample -> normal/count=0."""
        state = ClockState()
        for i in range(3):
            self.accept(state, str(i))
        self.assertEqual((state.status, state.exceeded), ('clock_skew_suspected', 3))
        self.accept(state, 'normal', 0)
        self.assertEqual((state.status, state.exceeded), ('normal', 0))

    def test_sleep_reset_and_late_response(self):
        """Expected: resume clears baseline/count/pending; late response ignored; no jump; fresh sample needed."""
        state = ClockState()
        self.accept(state, 'first')
        state.request('before-sleep', 110, 110)
        state.resume()
        self.assertEqual((state.status, state.exceeded, state.baseline), ('unknown', 0, None))
        self.assertIsNone(state.response('before-sleep', 150, 160, 3600220, 220))
        self.assertEqual(state.events, ['resume'])
        self.accept(state, 'after-sleep', 0)
        self.assertEqual(state.status, 'normal')

    def test_reverse_time_reset(self):
        """Expected: backward wall clock -> jump event, unknown, no stale baseline."""
        state = ClockState()
        self.accept(state, 'normal', 0)
        state.observe(-3000, 210)
        self.assertEqual((state.events, state.status, state.baseline), (['clock_jump'], 'unknown', None))

    def test_large_rtt_breaks_streak(self):
        """Expected: excessive RTT invalidates streak; next high offset counts as first."""
        state = ClockState()
        self.accept(state, 'one')
        self.accept(state, 'two')
        state.request('slow', 0, 0)
        self.assertIsNone(state.response('slow', 6150, 6160, 310, 310))
        self.assertEqual((state.status, state.exceeded), ('unknown', 0))
        self.accept(state, 'fresh')
        self.assertEqual((state.status, state.exceeded), ('unknown', 1))

    def test_timeout_and_stale_id(self):
        """Expected: timeout -> unknown/count=0, old ID cannot consume the next pending request."""
        state = ClockState()
        self.accept(state, 'one')
        state.request('timeout', 0, 0)
        state.timeout()
        state.request('new', 0, 0)
        self.assertIsNone(state.response('timeout', 50, 60, 110, 110))
        self.assertEqual(state.pending[0], 'new')
        self.assertIsNotNone(state.response('new', 50, 60, 110, 110))
        self.assertEqual((state.status, state.exceeded), ('normal', 0))

    def test_elapsed_timeout_even_if_callback_delayed(self):
        """Expected: 2s total timeout rejects a response even when network RTT alone is acceptable."""
        state = ClockState()
        state.request('slow-server', 0, 0)
        self.assertIsNone(state.response('slow-server', 50, 2050, 2100, 2100))
        self.assertEqual((state.status, state.exceeded, state.pending), ('unknown', 0, None))
