"""Synthetic specification checks; no production API/agent is implemented."""
import copy
import hashlib
import json
import platform
import re
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def pairs(items):
    result = {}
    for k, v in items:
        if k in result:
            raise ValueError('duplicate key')
        result[k] = v
    return result


def number(s):
    if len(s) > 32:
        raise ValueError('numeric token length')
    n = Decimal(s)
    if not n.is_finite() or n.copy_abs() > Decimal('9007199254740991'):
        raise ValueError('numeric range')
    digits = list(n.as_tuple().digits)
    exponent = n.as_tuple().exponent
    while digits and digits[-1] == 0:
        digits.pop()
        exponent += 1
    if digits and exponent < -6:
        raise ValueError('numeric precision')
    return n


def decode(s):
    return json.loads(s, object_pairs_hook=pairs, parse_int=number,
                      parse_float=number, parse_constant=lambda _: number('NaN'))


def canonical(v):
    if v is None:
        return 'null'
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, (Decimal, int, float)):
        n = number(str(v))
        if n == 0:
            return '0'
        s = format(n, 'f')
        return s.rstrip('0').rstrip('.') if '.' in s else s
    if isinstance(v, str):
        if not all(32 <= ord(c) <= 126 for c in v):
            raise ValueError('ASCII')
        return json.dumps(v, separators=(',', ':'))
    if isinstance(v, list):
        return '[' + ','.join(canonical(x) for x in v) + ']'
    if isinstance(v, dict):
        return '{' + ','.join(canonical(k)+':'+canonical(v[k]) for k in sorted(v)) + '}'
    raise ValueError('type')


def digest(v):
    return hashlib.sha256(canonical(v).encode('ascii')).hexdigest()


def fields(v, required, optional=()):
    if not isinstance(v, dict) or not set(required) <= v.keys() or v.keys()-set(required)-set(optional):
        raise ValueError('fields')


def integer(v):
    return not isinstance(v, bool) and isinstance(v, (int, Decimal)) and v == int(v) and 0 <= v <= 9007199254740991


def validate(b, configs=None):
    canonical(b)
    fields(b, ('schema_version', 'machine_id', 'boot_session_id', 'batch_id', 'sensor_config_id', 'samples'))
    if b['schema_version'] != '0.1.0':
        raise ValueError('version')
    for k in ('machine_id', 'boot_session_id', 'batch_id', 'sensor_config_id'):
        if not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', b[k]):
            raise ValueError('uuid')
    if b['batch_id'][14] != '4' or b['batch_id'][19] not in '89ab':
        raise ValueError('uuid4')
    if not isinstance(b['samples'], list) or not 1 <= len(b['samples']) <= 3600:
        raise ValueError('samples')
    previous = -1
    if configs is None:
        configs = json.loads((ROOT/'contracts/fixtures/sensor-configs.json').read_text(encoding='utf-8'))
    config = configs[b['sensor_config_id']]
    if config['machine_id'] != b['machine_id']:
        raise ValueError('config owner')
    registry = config['sensors']
    for s in b['samples']:
        fields(s, ('sample_seq', 'observed_at', 'monotonic_ms', 'readings'))
        if not integer(s['sample_seq']) or s['sample_seq'] <= previous or not integer(s['monotonic_ms']):
            raise ValueError('sequence')
        previous = s['sample_seq']
        if not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z', s['observed_at']):
            raise ValueError('utc')
        datetime.strptime(s['observed_at'], '%Y-%m-%dT%H:%M:%S.%fZ')
        if not isinstance(s['readings'], list) or len(s['readings']) != len(registry):
            raise ValueError('coverage')
        seen = set()
        for r in s['readings']:
            fields(r, ('sensor_id', 'unit', 'quality', 'value'), ('reason',))
            if r['sensor_id'] in seen or r['sensor_id'] not in registry or registry[r['sensor_id']]['unit'] != r['unit']:
                raise ValueError('sensor/unit')
            seen.add(r['sensor_id'])
            q, v = r['quality'], r['value']
            if q not in ('ok', 'unsupported', 'missing', 'error'):
                raise ValueError('quality')
            if (registry[r['sensor_id']]['support'] == 'unsupported') != (q == 'unsupported'):
                raise ValueError('support')
            if q == 'ok':
                if isinstance(v, bool) or not isinstance(v, (Decimal, int, float)) or 'reason' in r:
                    raise ValueError('quality_value')
                if abs(v) > Decimal('1e12'):
                    raise ValueError('sensor numeric range')
            elif v is not None:
                raise ValueError('quality_value')
            if 'reason' in r and not re.fullmatch(r'[a-z0-9_]{1,64}', r['reason']):
                raise ValueError('reason')


class LedgerModel:
    def __init__(self):
        self.batches, self.samples = {}, set()

    def submit(self, b, sha, authenticated=True, fail=False):
        if not authenticated:
            return 401
        if digest(b) != sha:
            return 422
        key = (b['machine_id'], b['batch_id'])
        if key in self.batches:
            return 200 if self.batches[key] == canonical(b) else 409
        try:
            validate(b)
        except ValueError:
            return 422
        keys = {(b['machine_id'], b['boot_session_id'], int(s['sample_seq'])) for s in b['samples']}
        if keys & self.samples:
            return 409
        if fail:
            return 503
        self.batches[key] = canonical(b)
        self.samples |= keys
        return 200


def measure(t1, t2, t3, t4, m1, m4, resumed=False, matched=True, synced=True):
    d, p = m4-m1, t3-t2
    rtt = d-p
    if resumed or not matched or not synced or d < 0 or p < 0 or not 0 <= rtt <= 250 or abs(t4-t1-d) >= 2000:
        return None
    return ((t2-t1+t3-t4)/2, rtt, rtt/2+17)


class Checks(unittest.TestCase):
    def setUp(self):
        self.b = decode((ROOT/'contracts/fixtures/batch.json').read_text(encoding='utf-8'))

    def test_normal_unsupported_missing(self):
        validate(self.b)

    def test_bad_quality_value(self):
        self.b['samples'][0]['readings'][1]['value'] = 0
        with self.assertRaises(ValueError):
            validate(self.b)

    def test_unit_mismatch(self):
        self.b['samples'][0]['readings'][0]['unit'] = 'W'
        with self.assertRaises(ValueError):
            validate(self.b)

    def test_duplicate_sample(self):
        self.b['samples'].append(copy.deepcopy(self.b['samples'][0]))
        with self.assertRaises(ValueError):
            validate(self.b)

    def test_canonical_golden(self):
        a = decode('{"z":-0,"a":[1.00,1e1,"x"]}')
        expected = b'{"a":[1,10,"x"],"z":0}'
        self.assertEqual(canonical(a).encode(), expected)
        self.assertEqual(digest(a), hashlib.sha256(expected).hexdigest())

    def test_invalid_json_numbers(self):
        for raw in ('NaN', 'Infinity', '9007199254740992', '0.0000001', '{"x":1,"x":2}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                decode(raw)

    def test_replay_after_response_loss(self):
        api = LedgerModel()
        for _ in range(2):
            self.assertEqual(api.submit(self.b, digest(self.b)), 200)
        self.assertEqual(len(api.samples), 1)

    def test_same_id_changed_content(self):
        api = LedgerModel()
        api.submit(self.b, digest(self.b))
        self.b['samples'][0]['readings'][0]['value'] = 53
        self.assertEqual(api.submit(self.b, digest(self.b)), 409)

    def test_new_id_same_sample(self):
        api = LedgerModel()
        api.submit(self.b, digest(self.b))
        self.b['batch_id'] = '55555555-5555-4555-8555-555555555555'
        self.assertEqual(api.submit(self.b, digest(self.b)), 409)

    def test_no_partial_success(self):
        api = LedgerModel()
        s = copy.deepcopy(self.b['samples'][0])
        s['sample_seq'] = 1
        s['readings'][0]['value'] = None
        self.b['samples'].append(s)
        self.assertEqual(api.submit(self.b, digest(self.b)), 422)
        self.assertEqual((api.batches, api.samples), ({}, set()))

    def test_rollback_retry(self):
        api = LedgerModel()
        self.assertEqual(api.submit(self.b, digest(self.b), fail=True), 503)
        self.assertFalse(api.samples)
        self.assertEqual(api.submit(self.b, digest(self.b)), 200)

    def test_auth_hash_failure(self):
        api = LedgerModel()
        self.assertEqual(api.submit(self.b, digest(self.b), authenticated=False), 401)
        self.assertEqual(api.submit(self.b, '0'*64), 422)
        self.assertFalse(api.samples)

    def test_time_normal_and_sign(self):
        self.assertEqual(measure(0, 50, 60, 110, 0, 110), (0, 100, 67))
        self.assertEqual(measure(0, 6050, 6060, 110, 0, 110)[0], 6000)

    def test_rtt_boundary(self):
        self.assertIsNotNone(measure(0, 125, 135, 260, 0, 260))
        self.assertIsNone(measure(0, 150, 160, 310, 0, 310))

    def test_clock_reverse_sleep(self):
        self.assertIsNone(measure(0, 50, 60, -3000, 0, 110))
        self.assertIsNone(measure(0, 50, 60, 3600110, 0, 110, resumed=True))

    def test_time_id_server(self):
        self.assertIsNone(measure(0, 50, 60, 110, 0, 110, matched=False))
        self.assertIsNone(measure(0, 50, 60, 110, 0, 110, synced=False))


if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Checks))
    report = {
        'contract_version': '0.1.0-draft.2',
        'base_revision': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'source_state': 'working tree; exact tested source hashes below',
        'source_hashes': {p: hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in
                          ('contracts/README.md', 'contracts/fixtures/batch.json',
                           'contracts/fixtures/sensor-configs.json', 'tests/verify_contract.py')},
        'environment': platform.platform(), 'python': sys.version,
        'pc': 'local development PC; synthetic data, no sensors accessed',
        'executed_at_utc': datetime.now(timezone.utc).isoformat(),
        'reproduction': 'python tests/verify_contract.py',
        'expected': '16 checks pass; test assertions define expected values/status',
        'actual': {'run': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors)},
        'success': result.wasSuccessful(),
        'details': [{'test': str(t), 'traceback': trace} for t, trace in result.failures+result.errors],
        'limitations': ['single-process Python specification model', 'independent .NET/API validation pending',
                         'no real PostgreSQL, HTTP, concurrency, crash or sleep tests', 'A/C review and CI pending'],
    }
    target = ROOT/'tests/results/s1-01-local.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    sys.exit(not result.wasSuccessful())
