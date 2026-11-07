"""Disposable HTTPS integration helper. No device secrets are written or printed."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import psycopg
from psycopg.conninfo import make_conninfo
from backend.app import create_app
from backend.contract import canonical, digest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--schema', required=True)
    parser.add_argument('--cert')
    parser.add_argument('--key')
    parser.add_argument('--port', type=int)
    parser.add_argument('--batch')
    parser.add_argument('--machine', action='store_true')
    parser.add_argument('--cleanup', action='store_true')
    args = parser.parse_args()
    if not args.schema.startswith('test_live_') or not args.schema[10:].isalnum():
        raise ValueError('test schema required')
    base = os.environ['TEST_DATABASE_URL']
    dsn = make_conninfo(base, options=f'-c search_path={args.schema}')
    if args.cleanup:
        with psycopg.connect(base) as db:
            db.execute(f'DROP SCHEMA IF EXISTS {args.schema} CASCADE')
        return
    if args.machine:
        with psycopg.connect(dsn) as db:
            print(db.execute('SELECT machine_id FROM machines').fetchone()[0])
        return
    if args.batch:
        machine = str(uuid.UUID(args.batch))
        batch = json.loads((ROOT/'tests/fixtures/s1-03-batch.json').read_text(encoding='utf-8'))
        original_config = batch['sensor_config_id']
        batch.update(machine_id=machine, sensor_config_id=str(uuid.uuid4()))
        configs = json.loads((ROOT/'contracts/fixtures/sensor-configs.json').read_text(encoding='utf-8'))
        config = configs[original_config]
        config['machine_id'] = machine
        with psycopg.connect(dsn) as db:
            db.execute('INSERT INTO sensor_configs VALUES(%s,%s,%s::jsonb)',
                (batch['sensor_config_id'], machine, json.dumps(config)))
        print(json.dumps({'body':canonical(batch), 'sha256':digest(batch)}))
        return
    with psycopg.connect(base) as db:
        db.execute(f'CREATE SCHEMA {args.schema}')
    try:
        with psycopg.connect(dsn) as db:
            for migration in ('001_ingestion.sql', '002_registration.sql'):
                db.execute((ROOT/'database/migrations'/migration).read_text(encoding='utf-8'))
            db.execute("INSERT INTO user_sessions VALUES(%s,%s,CURRENT_TIMESTAMP+interval '1 hour',false)",
                (hashlib.sha256(('synthetic-live-'+args.schema).encode()).hexdigest(), uuid.uuid4()))
        import uvicorn
        uvicorn.run(create_app(dsn), host='127.0.0.1', port=args.port, ssl_certfile=args.cert,
                    ssl_keyfile=args.key, access_log=False, log_level='warning')
    finally:
        with psycopg.connect(base) as db:
            db.execute(f'DROP SCHEMA IF EXISTS {args.schema} CASCADE')


if __name__ == '__main__':
    main()
