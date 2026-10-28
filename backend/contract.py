"""S1-01 ascii-json-v1 decoder and batch validation; kept against golden vectors."""
import hashlib
import json
import re
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
