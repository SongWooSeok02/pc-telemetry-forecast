"""PostgreSQL integration for S3-03. Run against a disposable test database."""
import copy
import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import make_conninfo

from backend.aggregation import process_one
from backend.app import create_app
from backend.contract import canonical, decode, digest
from backend.registration import sha

ROOT = Path(__file__).resolve().parents[1]
SENSOR = 'cpu/package/temp'


@unittest.skipUnless(os.environ.get('TEST_DATABASE_URL'), 'PostgreSQL integration requires TEST_DATABASE_URL')
class AggregationIntegration(unittest.TestCase):
    def setUp(self):
        base = os.environ['TEST_DATABASE_URL']
        schema = 's303_' + uuid.uuid4().hex
        with psycopg.connect(base) as db:
            db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        self.addCleanup(self.drop_schema, base, schema)
        self.dsn = make_conninfo(base, options=f'-c search_path={schema} -c timezone=Asia/Seoul')
        now = datetime.now(timezone.utc)
        self.now = now.replace(minute=now.minute//5*5, second=0, microsecond=0)
        self.start = self.now - timedelta(hours=25)
        self.body = decode((ROOT/'tests/fixtures/s1-03-batch.json').read_text())
        self.machine = self.body['machine_id']
        self.url = f'/v1/machines/{self.machine}'
        self.client = TestClient(create_app(self.dsn, clock=lambda: self.now))
        self.addCleanup(self.client.close)
        owner, outsider = uuid.uuid4(), uuid.uuid4()
        with self.connect() as db:
            for migration in sorted((ROOT/'database/migrations').glob('*.sql')):
                db.execute(migration.read_text(encoding='utf-8-sig'))
            db.execute('INSERT INTO machines VALUES (%s,%s,false)', (self.machine, owner))
            for token, user in [('owner', owner), ('outsider', outsider)]:
                db.execute("INSERT INTO user_sessions VALUES (%s,%s,now()+interval '1 day',false)", (sha(token), user))
            db.execute("INSERT INTO device_credentials VALUES (%s,%s,now()+interval '1 day',false)",
                       (sha('device'), self.machine))
            config = decode((ROOT/'contracts/fixtures/sensor-configs.json').read_text())
            cid = self.body['sensor_config_id']
            db.execute('INSERT INTO sensor_configs VALUES (%s,%s,%s::jsonb)',
                       (cid, self.machine, canonical(config[cid])))

    @staticmethod
    def drop_schema(base, schema):
        with psycopg.connect(base) as db:
            db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))

    def connect(self):
        return psycopg.connect(self.dsn)

    def send(self, seq, timestamp, value):
        body = copy.deepcopy(self.body)
        body['batch_id'] = str(uuid.uuid4())
        sample = body['samples'][0]
        body['samples'] = [sample]
        sample.update(sample_seq=seq, observed_at=timestamp.strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
                      monotonic_ms=1000+seq*1000)
        reading = sample['readings'][0]
        reading['value'] = value
        if value is None:
            reading.update(quality='missing', reason='not_ready')
        reply = self.client.post('/v1/telemetry/batches', content=canonical(body), headers={
            'Authorization': 'Bearer device', 'Content-Type': 'application/json', 'X-Content-SHA256': digest(body)})
        self.assertEqual(reply.status_code, 200, reply.text)

    def read(self, resource, token='owner', **options):
        return self.client.get(self.url + resource, headers={'Authorization': 'Bearer '+token}, params={
            'sensor_id': SENSOR, 'start': self.start.isoformat(), 'end': (self.now+timedelta(minutes=1)).isoformat(),
            **options})

    def work(self, hook=None):
        with self.connect() as db:
            return process_one(db, after_read=hook)

    def chart(self, resolution, token='owner', **options):
        if resolution is not None:
            options['resolution'] = resolution
        return self.client.get(f'/v1/dashboard/machines/{self.machine}/sensors/{SENSOR}/history',
                               headers={'Authorization': 'Bearer '+token},
                               params=options)

    def drain(self):
        for _ in range(50):
            if not self.work():
                return
        self.fail('queue did not drain')

    def test_owner_scope_boundaries_late_arrival_and_version_race(self):
        self.send(0, self.start, 40)
        pre = self.read('/aggregates').json()['items'][0]
        self.assertEqual((pre['pending'], pre['updated_at']), (True, None))
        self.assertTrue(self.work(lambda _: self.send(1, self.start+timedelta(seconds=9), 60)))
        with self.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM measurement_aggregates').fetchone()[0], 0)
        self.send(2, self.start+timedelta(seconds=10), 80)
        self.send(3, self.now, 70)
        self.send(5, self.start+timedelta(seconds=3), None)
        self.drain()
        self.send(4, self.start+timedelta(seconds=2), 20)
        stale = self.read('/aggregates').json()['items'][0]
        self.assertEqual((stale['average'], stale['pending']), (60, True))
        self.drain()
        ten = self.read('/aggregates', bucket_seconds=10).json()['items']
        self.assertEqual([(r['average'], r['maximum'], r['valid_count']) for r in ten],
                         [(40, 60, 3), (80, 80, 1), (70, 70, 1)])
        minute = self.read('/aggregates').json()['items']
        self.assertEqual((minute[0]['average'], minute[0]['valid_count']), (50, 4))
        self.assertEqual([r['provisional'] for r in minute], [False, True])
        self.assertTrue(all(r['updated_at'] and not r['pending'] for r in minute))
        with self.connect() as db:
            db.execute('INSERT INTO predictions (machine_id,sensor_id,target_at,generated_at,value,model_version) '
                       'VALUES (%s,%s,%s,%s,55,%s)',
                       (self.machine, SENSOR, self.now, self.now, 'baseline'))
        self.assertEqual(self.read('/predictions').json()['items'][0]['value'], 55)
        self.assertEqual(len(self.read('/measurements').json()['items']), 6)
        for resource in ('', '/measurements', '/aggregates', '/predictions'):
            self.assertEqual(self.read(resource, 'outsider').status_code, 404)
            self.assertEqual(self.read(resource, 'device').status_code, 401)
        with self.connect() as db:
            db.execute('UPDATE machines SET deleted=true')
        self.assertEqual(self.read('/aggregates').status_code, 404)

    def test_retry_and_partition_guard(self):
        self.send(0, self.start, 40)
        old = self.now.date()-timedelta(days=9)
        partition = 'measurements_' + old.strftime('%Y%m%d')
        with self.connect() as db:
            db.execute('SELECT ensure_measurement_partitions(%s)', (old,))
            db.execute('UPDATE measurements SET received_at=%s',
                       (datetime.combine(old, datetime.min.time(), timezone.utc),))
        with self.assertLogs('backend.aggregation', level='WARNING'):
            self.assertTrue(self.work(lambda db: db.execute('SELECT 1/0')))
        with self.connect() as db:
            self.assertEqual(db.execute('SELECT max(attempts) FROM reaggregation_queue').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM measurement_aggregates').fetchone()[0], 0)
            db.execute('SELECT prune_telemetry(%s)', (self.now,))
            self.assertIsNotNone(db.execute('SELECT to_regclass(%s)', (partition,)).fetchone()[0])
            db.execute("UPDATE reaggregation_queue SET ready_at=clock_timestamp()-interval '1 second'")

        def prune_while_reading(_):
            with self.assertRaises(psycopg.errors.LockNotAvailable), self.connect() as maintenance:
                maintenance.execute("SET LOCAL lock_timeout = '200ms'")
                maintenance.execute('SELECT prune_telemetry(%s)', (self.now,))

        self.assertTrue(self.work(prune_while_reading))
        self.drain()
        with self.connect() as db:
            db.execute('SELECT prune_telemetry(%s)', (self.now,))
            self.assertIsNone(db.execute('SELECT to_regclass(%s)', (partition,)).fetchone()[0])
        self.assertEqual(self.read('/measurements').json()['items'], [])
        self.assertEqual(self.read('/aggregates').json()['items'][0]['average'], 40)
        with self.connect() as db:
            for days, expected in [(31, [60]), (91, [])]:
                db.execute('SELECT prune_telemetry_metadata(%s)', (self.start+timedelta(days=days),))
                widths = db.execute('SELECT DISTINCT bucket_seconds FROM measurement_aggregates').fetchall()
                self.assertEqual([width for width, in widths], expected)

    def test_dashboard_cursor_reports_pending_and_recomputed_history(self):
        self.send(0, self.start, 40)
        self.send(1, self.start+timedelta(seconds=60), 90)
        self.send(2, self.start+timedelta(seconds=61), 100)
        self.drain()
        bounds = {'from': self.start.isoformat(), 'to': (self.start+timedelta(hours=1)).isoformat()}
        initial = self.chart('5m', **bounds)
        self.assertEqual(initial.status_code, 200, initial.text)
        point, = initial.json()['points']
        self.assertAlmostEqual(point['value'], 230/3)
        self.assertEqual((point['sampleCount'], point['max']), (3, 100))
        cursor = initial.json()['nextCursor']
        self.assertEqual(self.chart('5m', 'outsider', cursor=cursor).status_code, 404)
        self.assertEqual(self.chart('1m', cursor=cursor).status_code, 422)
        unchanged = self.chart(None, cursor=cursor).json()
        self.assertEqual((unchanged['points'], unchanged['unit']), ([], 'Cel'))
        raw_cursor = self.chart('raw', **bounds).json()['nextCursor']
        self.assertEqual(self.chart(None, cursor=raw_cursor).json()['unit'], 'Cel')
        self.send(3, self.start+timedelta(seconds=1), 10)
        raw, = self.chart('raw', cursor=raw_cursor).json()['points']
        self.assertEqual(raw['value'], 10)
        pending = self.chart('5m', cursor=cursor).json()
        self.assertTrue(pending['aggregationPending'])
        self.assertTrue(pending['points'][0]['provisional'])
        self.drain()
        updated = self.chart('5m', cursor=pending['nextCursor']).json()
        point, = updated['points']
        self.assertEqual((point['value'], point['sampleCount'], point['max']), (60, 4, 100))
        self.assertFalse(updated['aggregationPending'])
