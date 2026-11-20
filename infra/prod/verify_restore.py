"""Run against two empty, disposable databases using synthetic data only."""
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import subprocess
import tempfile
from uuid import uuid4

from fastapi.testclient import TestClient
import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.types.json import Jsonb

from backend.aggregation import process_one
from backend.app import create_app
from backend.contract import canonical, decode, digest
from backend.forecast import run_cycle, slot_at
from backend.registration import sha


def require(actual, expected, scenario):
    if actual != expected:
        raise RuntimeError(f'{scenario}: unexpected result')
    print(f'{scenario}: pass', flush=True)


def main():
    root = Path(os.environ['S402_SOURCE_ROOT'])
    source = os.environ['S402_SOURCE_DSN']
    restore = os.environ['S402_RESTORE_DSN']
    source_app = os.environ['S402_SOURCE_APP_DSN']
    restore_app = os.environ['S402_RESTORE_APP_DSN']
    for dsn in (source, restore, source_app, restore_app):
        if not conninfo_to_dict(dsn)['dbname'].startswith('s402_verify_'):
            raise ValueError('Use dedicated s402_verify_* databases')
    if conninfo_to_dict(source)['dbname'] == conninfo_to_dict(restore)['dbname']:
        raise ValueError('Source and restore must use different database names')
    for dsn in (source, restore):
        with psycopg.connect(dsn) as db:
            require(db.execute("SELECT count(*) FROM pg_tables WHERE schemaname='public'").fetchone()[0],
                    0, 'empty database guard')

    now = datetime.now(timezone.utc)
    body = decode((root / 'tests/fixtures/s1-03-batch.json').read_text(encoding='utf-8'))
    configs = json.loads((root / 'contracts/fixtures/sensor-configs.json').read_text(encoding='utf-8'))
    sensor = next(r['sensor_id'] for r in body['samples'][0]['readings'] if r['quality'] == 'ok')
    unit = next(r['unit'] for r in body['samples'][0]['readings'] if r['sensor_id'] == sensor)
    machine = body['machine_id']
    owner = uuid4()
    for index, sample in enumerate(body['samples']):
        at = now - timedelta(seconds=len(body['samples']) - index - 1)
        sample['observed_at'] = at.isoformat(timespec='microseconds').replace('+00:00', 'Z')
    owner_header = {'Authorization': 'Bearer synthetic-owner'}
    other_header = {'Authorization': 'Bearer synthetic-other'}
    headers = {'Authorization': 'Bearer synthetic-live', 'Content-Type': 'application/json',
               'X-Content-SHA256': digest(body)}
    revoked = {**headers, 'Authorization': 'Bearer synthetic-revoked'}
    artifact = b'{"kind":"persistence"}'

    with tempfile.TemporaryDirectory() as directory:
        scratch = Path(directory)
        models = scratch / 'models'
        models.mkdir()
        (models / 'model.json').write_bytes(artifact)
        with psycopg.connect(source, autocommit=True) as db:
            for path in sorted((root / 'database/migrations').glob('*.sql')):
                db.execute(path.read_text(encoding='utf-8-sig'))
            db.execute(Path(__file__).with_name('grants.sql').read_text(encoding='utf-8'))
            db.execute('INSERT INTO machines VALUES (%s,%s,false)', (machine, owner))
            db.execute('INSERT INTO user_sessions VALUES (%s,%s,%s,false)',
                       (sha('synthetic-owner'), owner, now + timedelta(days=1)))
            db.execute('INSERT INTO user_sessions VALUES (%s,%s,%s,false)',
                       (sha('synthetic-other'), uuid4(), now + timedelta(days=1)))
            db.execute('INSERT INTO device_credentials VALUES (%s,%s,%s,false)',
                       (sha('synthetic-live'), machine, now + timedelta(days=1)))
            for cid, cfg in configs.items():
                db.execute('INSERT INTO sensor_configs VALUES (%s,%s,%s)',
                           (cid, cfg['machine_id'], Jsonb(cfg)))
            db.execute('INSERT INTO forecast_targets VALUES (%s,%s,%s)', (machine, sensor, unit))
            db.execute('INSERT INTO forecast_models VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,true)',
                       (machine, sensor, 'verify-v1', 'synthetic-run', now-timedelta(days=2),
                        now-timedelta(days=1), now, 'model.json', hashlib.sha256(artifact).hexdigest(),
                        Jsonb({'window_seconds': 1, 'step_seconds': 1, 'horizon_seconds': 60, 'unit': unit}),
                        Jsonb({'mae': 1})))
        with TestClient(create_app(source_app, clock=lambda: now)) as client:
            code = client.post('/v1/registration-codes', headers=owner_header)
            require(code.status_code, 200, 'registration code with runtime role')
            device = client.post('/v1/device/registrations',
                                 json={'registration_code': code.json()['registration_code']})
            require(device.status_code, 200, 'device registration with runtime role')
            revoked['Authorization'] = 'Bearer ' + device.json()['device_token']
            require(client.post(f"/v1/machines/{device.json()['machine_id']}/revoke",
                                headers=owner_header).status_code, 200, 'device revocation')
            accepted = client.post('/v1/telemetry/batches', content=canonical(body), headers=headers)
            require(accepted.status_code, 200, 'source ingestion')
            require(client.get('/health/partitions').status_code, 200, 'partition horizon')
            with psycopg.connect(source_app) as db:
                while process_one(db):
                    db.commit()
                require(run_cycle(db, models, clock=lambda: now), True, 'forecast worker slot')
            path = f'/v1/machines/{machine}/predictions'
            window = {'sensor_id': sensor, 'start': (now - timedelta(minutes=2)).isoformat(),
                      'end': (now + timedelta(minutes=2)).isoformat()}
            predictions = client.get(path, headers=owner_header, params=window).json()['items']
            require(len(predictions), 1, 'source prediction query')
            require((predictions[0]['model_version'], predictions[0]['status']),
                    ('verify-v1', 'ok'), 'active model prediction')
            machines = client.get('/v1/machines', headers=owner_header).json()

        # libpq environment keeps connection passwords out of subprocess arguments.
        def pg(command, dsn, *args):
            connection = conninfo_to_dict(dsn)
            env = {key: value for key, value in os.environ.items() if not key.startswith('PG')}
            for key in ('host', 'port', 'user', 'password', 'dbname', 'passfile', 'sslmode'):
                if key in connection:
                    env['PG' + {'dbname': 'DATABASE'}.get(key, key.upper())] = connection[key]
            subprocess.run([command, *args], env=env, check=True)

        dump = scratch / 'db.dump'
        pg('pg_dump', source, '--format=custom', '--file', str(dump))
        pg('pg_restore', restore, '--dbname', conninfo_to_dict(restore)['dbname'],
           '--single-transaction', '--exit-on-error', str(dump))
        shutil.copytree(models, scratch / 'restored-models')
        print(f'logical restore: pass ({dump.stat().st_size} bytes)', flush=True)

        tables = ('machines', 'device_credentials', 'ingestion_batches', 'ingestion_samples',
                  'measurements', 'measurement_aggregates', 'forecast_models', 'predictions')
        with psycopg.connect(source) as before, psycopg.connect(restore) as after:
            for table in tables:
                query = f'SELECT to_jsonb(t) FROM {table} t ORDER BY to_jsonb(t)'
                rows = before.execute(query).fetchall()
                require(bool(rows), True, f'{table} contains fixture data')
                require(after.execute(query).fetchall(), rows, f'{table} rows identical')
        with TestClient(create_app(restore_app, clock=lambda: now)) as client:
            replay = client.post('/v1/telemetry/batches', content=canonical(body), headers=headers)
            require((replay.status_code, replay.json()), (200, accepted.json()), 'duplicate replay response')
            require(client.post('/v1/telemetry/batches', content=canonical(body), headers=revoked).status_code,
                    401, 'revoked device rejected after restore')
            require(client.get('/v1/machines', headers=owner_header).json(), machines, 'owner machine query')
            require(client.get(path, headers=owner_header, params=window).json()['items'],
                    predictions, 'owner prediction query')
            require(client.get(path, headers=other_header, params=window).status_code,
                    404, 'other owner cannot query restored predictions')
        with psycopg.connect(source) as before, psycopg.connect(restore_app) as after:
            for table in ('ingestion_batches', 'ingestion_samples', 'measurements', 'predictions'):
                query = f'SELECT count(*) FROM {table}'
                require(after.execute(query).fetchone(), before.execute(query).fetchone(),
                        f'{table} count unchanged after replay')
            require(run_cycle(after, scratch / 'restored-models', clock=lambda: now), False,
                    'forecast worker restart preserves slot')
            require(hashlib.sha256((scratch / 'restored-models/model.json').read_bytes()).hexdigest(),
                    hashlib.sha256(artifact).hexdigest(), 'restored model SHA-256')
            next_slot = slot_at(now) + timedelta(seconds=10)
            require(run_cycle(after, scratch / 'restored-models', clock=lambda: next_slot),
                    True, 'restored forecast runs next slot')
            require(after.execute('SELECT model_version,status FROM predictions ORDER BY slot_at DESC LIMIT 1')
                    .fetchone(), ('verify-v1', 'ok'), 'next slot uses restored model artifact')


if __name__ == '__main__':
    main()
