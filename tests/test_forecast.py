"""Forecast integration against a disposable PostgreSQL schema."""
import hashlib
import os
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

SENSOR = 'cpu/package/temp'
UNIT = 'C'
REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = REPO_ROOT / 'database/migrations'


class ForecastMigrationOrderTests(unittest.TestCase):
    def test_compose_migrate_applies_all_migrations_in_order(self):
        compose = (REPO_ROOT / 'infra/compose.yaml').read_text(encoding='utf-8')
        applied = re.findall(r'/migrations/(\d+_[a-z0-9_]+\.sql)', compose)
        expected = [path.name for path in sorted(MIGRATIONS.glob('*.sql'))]
        self.assertEqual(applied, expected)
        self.assertEqual(applied[-1], '006_prediction_unit.sql')

    def test_006_migration_is_alter_only_without_backfill(self):
        sql_text = (MIGRATIONS / '006_prediction_unit.sql').read_text(encoding='utf-8')
        self.assertIn('ALTER TABLE predictions ADD COLUMN unit text', sql_text)
        self.assertNotIn('CREATE TABLE', sql_text)
        self.assertNotIn('UPDATE predictions', sql_text)


@unittest.skipUnless(os.environ.get('TEST_DATABASE_URL'), 'requires PostgreSQL')
class ForecastIntegration(unittest.TestCase):
    def setUp(self):
        import psycopg
        from fastapi.testclient import TestClient
        from psycopg import sql
        from psycopg.conninfo import make_conninfo
        from backend.app import create_app

        self.psycopg = psycopg
        self.sql = sql
        self.base = os.environ['TEST_DATABASE_URL']
        self.schema = 's305_' + uuid4().hex
        self.dsn = make_conninfo(self.base, options=f'-c search_path={self.schema}')
        with psycopg.connect(self.base) as db:
            db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        self.addCleanup(self.drop_schema)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.now = datetime.now(timezone.utc).replace(second=30, microsecond=0)
        self.machine, self.other, self.owner, self.boot, self.config = (uuid4() for _ in range(5))
        from backend.registration import sha
        with self.connect() as db:
            for path in sorted(MIGRATIONS.glob('*.sql')):
                db.execute(path.read_text(encoding='utf-8-sig'))
            for machine, owner in ((self.machine, self.owner), (self.other, uuid4())):
                db.execute('INSERT INTO machines VALUES (%s,%s,false)', (machine, owner))
                db.execute('INSERT INTO forecast_targets VALUES (%s,%s,%s)', (machine, SENSOR, UNIT))
            db.execute("INSERT INTO user_sessions VALUES (%s,%s,now()+interval '1 day',false)",
                       (sha('owner'), self.owner))
        self.client = TestClient(create_app(self.dsn, clock=lambda: self.now))
        self.addCleanup(self.client.close)

    def drop_schema(self):
        with self.psycopg.connect(self.base) as db:
            db.execute(self.sql.SQL('DROP SCHEMA {} CASCADE').format(self.sql.Identifier(self.schema)))

    def connect(self):
        return self.psycopg.connect(self.dsn)

    def measure(self, machine, at, value, quality='ok'):
        with self.connect() as db:
            db.execute("""
                INSERT INTO measurements
                    (received_at,machine_id,boot_session_id,sample_seq,batch_id,sensor_config_id,
                     sensor_id,observed_at,monotonic_ms,unit,quality,value)
                VALUES (%s,%s,%s,0,%s,%s,%s,%s,0,%s,%s,%s)
            """, (at, machine, self.boot, uuid4(), self.config, SENSOR, at, UNIT, quality, value))

    def model(self, version, weight):
        from psycopg.types.json import Jsonb
        blob = f'{{"kind":"linear","weights":[{weight},1],"bias":0}}'.encode()
        path = self.root / f'{version}.json'
        path.write_bytes(blob)
        with self.connect() as db:
            db.execute("""
                INSERT INTO forecast_models
                    (machine_id,sensor_id,version,training_run_id,trained_from,trained_until,
                     artifact_path,artifact_sha256,input_spec,evaluation)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (self.machine, SENSOR, version, f'train-{version}', self.now-timedelta(days=2), self.now-timedelta(days=1),
                  path.name, hashlib.sha256(blob).hexdigest(),
                  Jsonb({'window_seconds': 2, 'step_seconds': 1, 'horizon_seconds': 60, 'unit': UNIT}),
                  Jsonb({'mae': 1})))
            db.execute('SELECT activate_forecast_model(%s,%s,%s)', (self.machine, SENSOR, version))
        return path

    def cycle(self):
        from backend.forecast import run_cycle
        with self.connect() as db:
            return run_cycle(db, self.root, clock=lambda: self.now)

    def read(self, machine=None):
        return self.client.get(f'/v1/machines/{machine or self.machine}/predictions',
                               headers={'Authorization': 'Bearer owner'},
                               params={'sensor_id': SENSOR, 'start': (self.now-timedelta(minutes=2)).isoformat(),
                                       'end': (self.now+timedelta(minutes=2)).isoformat()})

    def test_restart_model_switch_gap_and_owner_scope(self):
        from backend.forecast import BASELINE, NO_MODEL
        self.model('v1', 2)
        self.measure(self.machine, self.now-timedelta(seconds=1), 10)
        self.measure(self.machine, self.now, 20)
        self.measure(self.other, self.now, 900)
        self.assertEqual(self.read().json()['items'], [])
        self.assertTrue(self.cycle())
        self.assertFalse(self.cycle())  # new connection, same durable slot
        self.now += timedelta(seconds=10)
        bad_file = self.model('v2', 3)
        self.measure(self.machine, self.now-timedelta(seconds=2), 10)
        self.measure(self.machine, self.now, 30)
        self.assertTrue(self.cycle())  # one missing second
        self.now += timedelta(seconds=10)
        self.measure(self.machine, self.now-timedelta(seconds=1), 10)
        self.measure(self.machine, self.now, 40)
        self.assertTrue(self.cycle())
        self.now += timedelta(seconds=10)
        bad_file.write_bytes(b'altered artifact')
        self.measure(self.machine, self.now, 45)
        with self.assertLogs('backend.forecast', level='WARNING'):
            self.assertTrue(self.cycle())
        self.now += timedelta(seconds=10)
        self.measure(self.machine, self.now, None, 'missing')
        self.assertTrue(self.cycle())
        self.now += timedelta(seconds=20)
        self.assertTrue(self.cycle())  # recovery skips the missed slot
        items = self.read().json()['items']
        self.assertEqual([(r['model_version'], r['value'], r['status'], r['reason']) for r in items],
                         [('v1', 40, 'ok', None), (BASELINE, 30, 'fallback', 'invalid_window'),
                          ('v2', 70, 'ok', None), (BASELINE, 45, 'fallback', 'model_rejected'),
                          (NO_MODEL, None, 'unavailable', 'invalid_latest_input'),
                          (NO_MODEL, None, 'unavailable', 'stale_or_missing_input')])
        for row in items:
            self.assertEqual(row['unit'], UNIT)
            self.assertEqual(datetime.fromisoformat(row['target_at']) - datetime.fromisoformat(row['generated_at']),
                             timedelta(seconds=60))
        self.assertEqual(self.read().json()['items'], items)
        self.assertEqual(self.read(self.other).status_code, 404)
        with self.connect() as db:
            self.assertEqual(db.execute('SELECT value, status FROM predictions WHERE machine_id=%s ORDER BY slot_at',
                                        (self.other,)).fetchall()[0], (900, 'fallback'))
            self.assertEqual(db.execute('SELECT version FROM forecast_models WHERE active').fetchall(), [('v2',)])
        with self.assertRaises(self.psycopg.errors.RaiseException), self.connect() as db:
            db.execute('SELECT activate_forecast_model(%s,%s,%s)', (self.machine, SENSOR, 'unknown'))
        with self.assertRaises(self.psycopg.errors.RaiseException), self.connect() as db:
            db.execute("UPDATE forecast_models SET artifact_path='replacement.json' WHERE version='v2'")
        with self.connect() as db:
            self.assertEqual(db.execute('SELECT version FROM forecast_models WHERE active').fetchall(), [('v2',)])

    def test_slow_worker_excludes_overlap_and_failed_transaction_can_retry(self):
        from backend.forecast import run_cycle
        self.measure(self.machine, self.now, 42)
        with self.connect() as db:
            db.execute('SELECT pg_advisory_xact_lock(1030501)')
            self.assertFalse(self.cycle())
        with self.assertRaises(RuntimeError), self.connect() as db:
            def failing_clock():
                if db.execute('SELECT count(*) FROM predictions').fetchone()[0]:
                    raise RuntimeError('simulated interruption')
                return self.now
            run_cycle(db, self.root, clock=failing_clock)
        self.assertEqual(self.read().json()['items'], [])
        self.assertTrue(self.cycle())
        self.assertEqual(self.read().json()['items'][0]['value'], 42)
        self.assertEqual(self.read().json()['items'][0]['unit'], UNIT)

    def test_legacy_prediction_without_unit_is_returned_as_none(self):
        from backend.forecast import BASELINE
        legacy_gen = self.now - timedelta(seconds=90)
        legacy_target = legacy_gen + timedelta(seconds=60)
        with self.connect() as db:
            db.execute("""
                INSERT INTO predictions
                    (machine_id,sensor_id,target_at,generated_at,value,model_version,status,reason)
                VALUES (%s,%s,%s,%s,%s,%s,'ok',NULL)
            """, (self.machine, SENSOR, legacy_target, legacy_gen, 38.5, 'legacy-v0'))
            db.execute('UPDATE forecast_targets SET unit=%s WHERE machine_id=%s AND sensor_id=%s',
                       ('K', self.machine, SENSOR))
        self.measure(self.machine, self.now, 315.15)
        with self.connect() as db:
            db.execute('UPDATE measurements SET unit=%s WHERE machine_id=%s AND sensor_id=%s',
                       ('K', self.machine, SENSOR))
        self.assertTrue(self.cycle())
        items = self.read().json()['items']
        self.assertEqual([(r['model_version'], r['unit']) for r in items],
                         [('legacy-v0', None), (BASELINE, 'K')])
