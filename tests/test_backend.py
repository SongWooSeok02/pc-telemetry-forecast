"""Run: python -m unittest tests/test_backend.py is not required; use discovery."""
import copy
import hashlib
import json
import os
import sys
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path

import psycopg
from psycopg.conninfo import make_conninfo
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.app import create_app
from backend.contract import decode, canonical, digest, validate


class ContractParity(unittest.TestCase):
    def test_fixture_and_numbers(self):
        """Backend accepts the S1-01 synthetic fixture and canonical numeric vectors."""
        body = decode((ROOT/'contracts/fixtures/batch.json').read_text(encoding='utf-8'))
        validate(body)
        self.assertEqual(canonical(decode('{"z":-0,"a":[1.00,1e1,"x"]}')), '{"a":[1,10,"x"],"z":0}')

    def test_golden_vectors(self):
        """Backend canonical bytes and hashes match every independently pinned S1-01 vector."""
        vectors = json.loads((ROOT/'contracts/fixtures/hash-vectors.json').read_text(encoding='utf-8'))
        for vector in vectors:
            with self.subTest(vector=vector['name']):
                body = decode(vector['input'])
                self.assertEqual(canonical(body), vector['canonical'])
                self.assertEqual(digest(body), vector['sha256'])

    def test_unauthenticated(self):
        """Missing credentials return 401 with contract envelope before database access."""
        with TestClient(create_app('not-a-dsn')) as client:
            response = client.post('/v1/telemetry/batches', json={})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers['www-authenticate'], 'Bearer')
        self.assertEqual(response.json()['error']['code'], 'DEVICE_UNAUTHENTICATED')


@unittest.skipUnless(os.environ.get('TEST_DATABASE_URL'), 'TEST_DATABASE_URL missing: PostgreSQL integration NOT executed')
class PostgreSQLIngestion(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = 'test_s103_' + uuid.uuid4().hex
        cls.base = os.environ['TEST_DATABASE_URL']
        with psycopg.connect(cls.base) as db:
            db.execute(f'CREATE SCHEMA {cls.schema}')
        cls.dsn = make_conninfo(cls.base, options=f'-c search_path={cls.schema} -c timezone=Asia/Seoul')
        with psycopg.connect(cls.dsn) as db:
            db.execute((ROOT/'database/migrations/001_ingestion.sql').read_text(encoding='utf-8-sig'))
            db.execute((ROOT/'database/migrations/003_quality.sql').read_text(encoding='utf-8'))

    @classmethod
    def tearDownClass(cls):
        with psycopg.connect(cls.base) as db:
            db.execute(f'DROP SCHEMA {cls.schema} CASCADE')

    def setUp(self):
        self.body = decode((ROOT/'tests/fixtures/s1-03-batch.json').read_text(encoding='utf-8'))
        self.token = 'synthetic-test-only'
        self.now = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
        self.fail_commit = False
        def hook(db):
            if self.fail_commit:
                db.execute('SELECT 1/0')  # Real DB error after all inserts.
        self.app = create_app(self.dsn, clock=lambda: self.now, before_commit=hook)
        self.client = TestClient(self.app)
        configs = json.loads((ROOT/'contracts/fixtures/sensor-configs.json').read_text(encoding='utf-8'))
        with psycopg.connect(self.dsn) as db:
            db.execute('TRUNCATE measurements,ingestion_samples,ingestion_batches,reaggregation_queue,device_credentials,sensor_configs')
            db.execute('INSERT INTO device_credentials VALUES(%s,%s,%s,false)',
                (hashlib.sha256(self.token.encode()).hexdigest(), self.body['machine_id'], self.now+timedelta(days=365)))
            for cid, cfg in configs.items():
                db.execute('INSERT INTO sensor_configs VALUES(%s,%s,%s::jsonb)', (cid,cfg['machine_id'],json.dumps(cfg)))

    def test_device_time_trust_and_authentication(self):
        """Authenticated time exchange echoes the ID and defers when unsynchronized or reversed."""
        request = dict(schema_version='0.1.0', request_id=str(uuid.uuid4()))
        headers = {'Authorization': 'Bearer '+self.token}
        endpoint = '/v1/device/time'
        client = TestClient(create_app(self.dsn, clock=lambda: self.now, clock_precision=lambda: 1000))
        self.assertEqual(client.post(endpoint, json=request).status_code, 401)
        response = client.post(endpoint, json=request, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), dict(request, clock_status='synchronized', precision_us=1000,
            server_received_at=self.now.strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
            server_sent_at=self.now.strftime('%Y-%m-%dT%H:%M:%S.%fZ')))
        times = iter((self.now, self.now-timedelta(seconds=2)))
        reversed_clock = TestClient(create_app(self.dsn, clock=lambda: next(times), clock_precision=lambda: 1000))
        for untrusted in (reversed_clock, TestClient(create_app(self.dsn, clock_precision=lambda: None))):
            response = untrusted.post(endpoint, json=request, headers=headers)
            self.assertEqual(response.status_code, 503)
            error = response.json()['error']
            self.assertEqual((error['code'], error['request_id'], error['retryable']),
                ('CLOCK_UNTRUSTED', request['request_id'], True))

    def test_quality_buckets_preserve_delayed_observation(self):
        """A 24h delayed batch stays in its observed bucket and replay does not inflate quality counts."""
        observed = self.now - timedelta(days=1)
        sample = self.body['samples'][0]
        sample['observed_at'] = observed.strftime('%Y-%m-%dT%H:%M:%S.%fZ')
        sensor = next(r for r in sample['readings'] if r['quality'] == 'ok')
        sensor.update(quality='error', value=None, reason='probe_error')
        response = self.send()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.send().json(), response.json())
        with psycopg.connect(self.dsn) as db:
            times = db.execute('SELECT DISTINCT observed_at, received_at, monotonic_ms FROM measurements').fetchall()
            buckets = db.execute('SELECT bucket_seconds, bucket_start, sum(ok_count), sum(missing_count), '
                'sum(error_count), sum(unsupported_count) FROM sensor_quality_buckets '
                'GROUP BY bucket_seconds, bucket_start ORDER BY bucket_seconds').fetchall()
        self.assertEqual(times, [(observed, self.now, sample['monotonic_ms'])])
        counts = tuple(sum(r['quality'] == q for r in sample['readings'])
            for q in ('ok', 'missing', 'error', 'unsupported'))
        self.assertEqual(buckets, [(width, observed, *counts) for width in (10, 60)])

    def send(self, body=None, client=None, **headers):
        body = body or self.body
        return (client or self.client).post('/v1/telemetry/batches', content=canonical(body),
            headers={'Authorization': 'Bearer '+self.token, 'Content-Type': 'application/json',
                     'X-Content-SHA256': digest(body), **headers})

    def counts(self):
        with psycopg.connect(self.dsn) as db:
            return tuple(db.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in
                ('ingestion_batches','ingestion_samples','measurements','reaggregation_queue'))

    def test_replay_response_loss_and_midnight(self):
        """Cut the response after commit; replay next UTC day returns the stored response with unchanged rows."""
        async def disconnect(scope, receive, send):
            if scope['type'] != 'http':
                return await self.app(scope, receive, send)

            async def drop_response(message):
                self.assertEqual(message['type'], 'http.response.start')
                self.assertEqual(message['status'], 200)
                raise ConnectionError('response dropped after commit')

            await self.app(scope, receive, drop_response)

        with TestClient(disconnect) as client, self.assertRaises(ConnectionError):
            self.send(client=client)
        before = self.counts()
        self.assertEqual(before, (1, 1, 3, 6))
        with psycopg.connect(self.dsn) as db:
            stored = db.execute('SELECT response FROM ingestion_batches').fetchone()[0]
        self.now += timedelta(days=1)
        second = self.send()
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json(), stored)
        self.assertEqual(self.counts(), before)

    def test_concurrent_replay(self):
        """Eight concurrent identical requests return one committed batch and identical 200 responses."""
        def submit(_):
            with TestClient(self.app) as client:
                return self.send(client=client)
        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(submit, range(8)))
        self.assertEqual([r.status_code for r in responses], [200]*8)
        self.assertTrue(all(r.json() == responses[0].json() for r in responses))
        self.assertEqual(self.counts()[:3], (1,1,len(self.body['samples'][0]['readings'])))

    def test_conflicts_and_atomic_overlap(self):
        """Changed batch returns BATCH_CONFLICT; another batch with overlapping sample rolls back all writes."""
        self.assertEqual(self.send().status_code, 200)
        before = self.counts()
        changed = copy.deepcopy(self.body)
        changed['samples'][0]['readings'][0]['value'] += 1
        self.assertEqual(self.send(changed).json()['error']['code'], 'BATCH_CONFLICT')
        changed['batch_id'] = str(uuid.uuid4())
        self.assertEqual(self.send(changed).json()['error']['code'], 'SAMPLE_CONFLICT')
        self.assertEqual(self.counts(), before)

    def test_concurrent_overlapping_batches(self):
        """Two concurrent different batch IDs sharing a sample yield one 200, one 409, and one ledger entry."""
        other = copy.deepcopy(self.body)
        other['batch_id'] = str(uuid.uuid4())
        def submit(body):
            with TestClient(self.app) as client:
                return self.send(body,client=client)
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(submit,[self.body,other]))
        self.assertEqual(sorted(r.status_code for r in responses),[200,409])
        self.assertEqual(self.counts()[:2],(1,1))

    def test_database_error_and_retry(self):
        """SQL failure before commit leaves all four tables empty; retry commits once."""
        self.fail_commit = True
        response = self.send()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.counts(), (0,0,0,0))
        self.fail_commit = False
        self.assertEqual(self.send().status_code, 200)

    def test_last_sample_invalid(self):
        """Invalid final sample returns 422 without ledger or measurement writes."""
        sample = copy.deepcopy(self.body['samples'][0])
        sample['sample_seq'] += 1
        sample['readings'][0]['value'] = None
        self.body['samples'].append(sample)
        self.assertEqual(self.send().status_code, 422)
        self.assertEqual(self.counts(), (0,0,0,0))

    def test_observed_anomaly_received_partition(self):
        """Old and future observed times are preserved; UTC received date chooses physical partition."""
        self.body['samples'][0]['observed_at'] = '2000-01-01T00:00:00.000000Z'
        sample = copy.deepcopy(self.body['samples'][0])
        sample['sample_seq'] += 1
        sample['observed_at'] = '2099-01-01T00:00:00.000000Z'
        self.body['samples'].append(sample)
        self.now = self.now.replace(hour=23,minute=59,second=59)
        self.assertEqual(self.send().status_code, 200)
        with psycopg.connect(self.dsn) as db:
            tables = db.execute('SELECT DISTINCT tableoid::regclass::text FROM measurements').fetchall()
            years = db.execute('SELECT DISTINCT extract(year from observed_at) FROM measurements ORDER BY 1').fetchall()
        self.assertEqual(tables, [('measurements_'+self.now.strftime('%Y%m%d'),)])
        self.assertEqual([int(r[0]) for r in years], [2000,2099])
        self.now += timedelta(seconds=1)
        self.body['batch_id'] = str(uuid.uuid4())
        for s in self.body['samples']:
            s['sample_seq'] += 10
        self.assertEqual(self.send().status_code, 200)
        with psycopg.connect(self.dsn) as db:
            self.assertEqual(db.execute('SELECT count(DISTINCT tableoid) FROM measurements').fetchone()[0], 2)

    def test_partitions_failure_monitor_and_rollback(self):
        """Health checks today+3; missing target partition alerts and insertion rolls back; repair is idempotent."""
        self.assertEqual(self.client.get('/health/partitions').status_code, 200)
        name = 'measurements_'+self.now.strftime('%Y%m%d')
        with psycopg.connect(self.dsn) as db:
            db.execute(f'DROP TABLE {name}')
        self.assertEqual(self.client.get('/health/partitions').status_code, 503)
        self.assertEqual(self.send().status_code, 503)
        self.assertEqual(self.counts(), (0,0,0,0))
        with psycopg.connect(self.dsn) as db:
            db.execute('SELECT ensure_measurement_partitions()')
            db.execute('SELECT ensure_measurement_partitions()')
        self.assertEqual(self.send().status_code, 200)

    def test_auth_config_hash_and_limits(self):
        """Foreign machine, missing config, bad hash, malformed JSON, oversized input and revoked tokens reject without writes."""
        b = copy.deepcopy(self.body)
        b['machine_id'] = str(uuid.uuid4())
        self.assertEqual(self.send(b).status_code, 403)
        b = copy.deepcopy(self.body)
        b['sensor_config_id'] = str(uuid.uuid4())
        self.assertEqual(self.send(b).json()['error']['code'], 'SENSOR_CONFIG_PENDING')
        self.assertEqual(self.send(**{'X-Content-SHA256':'0'*64}).json()['error']['code'], 'HASH_MISMATCH')
        for raw, status in [('{"x":1,"x":2}',400), ('x'*1048577,413)]:
            r = self.client.post('/v1/telemetry/batches',content=raw,
                headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json'})
            self.assertEqual(r.status_code,status)
        with psycopg.connect(self.dsn) as db:
            db.execute('UPDATE device_credentials SET revoked=true')
        self.assertEqual(self.send().status_code,401)
        self.assertEqual(self.counts(),(0,0,0,0))

    def test_retention_separate_from_measurements(self):
        """Raw deletion preserves batch replay and cross-batch sample uniqueness for device lifetime."""
        first = self.send()
        self.assertEqual(first.status_code,200)
        with psycopg.connect(self.dsn) as db:
            db.execute('DELETE FROM measurements')
        self.assertEqual(self.send().json(),first.json())
        self.body['batch_id'] = str(uuid.uuid4())
        self.assertEqual(self.send().json()['error']['code'],'SAMPLE_CONFLICT')
        self.assertEqual(self.counts()[:3],(1,1,0))


if __name__ == '__main__':
    unittest.main()
