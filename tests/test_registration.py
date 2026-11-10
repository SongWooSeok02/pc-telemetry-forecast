"""Real PostgreSQL lifecycle tests; all credentials here are synthetic."""
import hashlib
import json
import os
from pathlib import Path
import unittest
import uuid
import time
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor

import psycopg
from psycopg.conninfo import make_conninfo
from fastapi.testclient import TestClient
from backend.app import create_app
from backend.contract import canonical, digest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get('TEST_DATABASE_URL'), 'PostgreSQL required')
class Registration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = os.environ['TEST_DATABASE_URL']
        cls.schema = 'test_registration_' + uuid.uuid4().hex
        with psycopg.connect(cls.base) as db:
            db.execute(f'CREATE SCHEMA {cls.schema}')
        cls.dsn = make_conninfo(cls.base, options=f'-c search_path={cls.schema}')
        with psycopg.connect(cls.dsn) as db:
            for name in ('001_ingestion.sql', '002_registration.sql'):
                db.execute((ROOT/'database/migrations'/name).read_text(encoding='utf-8'))

    @classmethod
    def tearDownClass(cls):
        with psycopg.connect(cls.base) as db:
            db.execute(f'DROP SCHEMA {cls.schema} CASCADE')

    def setUp(self):
        self.user = uuid.uuid4()
        self.session = 'synthetic-user-' + uuid.uuid4().hex
        self.other = 'synthetic-other-' + uuid.uuid4().hex
        with psycopg.connect(self.dsn) as db:
            for token, user in ((self.session, self.user), (self.other, uuid.uuid4())):
                db.execute("INSERT INTO user_sessions VALUES(%s,%s,CURRENT_TIMESTAMP+interval '1 hour',false)",
                    (hashlib.sha256(token.encode()).hexdigest(), user))
        self.client = TestClient(create_app(self.dsn))
        self.addCleanup(self.client.close)

    def issue(self):
        response = self.client.post('/v1/registration-codes', headers={'Authorization': 'Bearer '+self.session})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        return response.json()['registration_code']

    def consume(self, code):
        return self.client.post('/v1/device/registrations', json={'registration_code': code})

    def manage(self, machine, action, token=None):
        return self.client.post(f'/v1/machines/{machine}/{action}',
            headers={'Authorization': 'Bearer '+(token or self.session)})

    def authorized(self, token):
        # Authentication precedes body validation, so 422 means a live device token.
        return self.client.post('/v1/telemetry/batches', json={},
            headers={'Authorization': 'Bearer '+token}).status_code

    def test_expiry_reuse_owner_and_separation(self):
        """Expired/reused codes fail; ownership cannot be supplied by the enrolling caller."""
        code = self.issue()
        with psycopg.connect(self.dsn) as db:
            db.execute("UPDATE registration_codes SET expires_at=CURRENT_TIMESTAMP-interval '1 second' WHERE code_sha256=%s",
                (hashlib.sha256(code.encode()).hexdigest(),))
        self.assertEqual(self.consume(code).status_code, 401)
        code = self.issue()
        response = self.consume(code)
        self.assertEqual(response.status_code, 200)
        device = response.json()
        self.assertEqual(self.consume(code).status_code, 401)
        self.assertEqual(self.authorized(self.session), 401)
        self.assertEqual(self.manage(device['machine_id'], 'delete', self.other).status_code, 404)
        self.assertEqual(self.manage(device['machine_id'], 'revoke', device['device_token']).status_code, 401)
        with psycopg.connect(self.dsn) as db:
            self.assertEqual(db.execute('SELECT owner_id FROM machines WHERE machine_id=%s',
                (device['machine_id'],)).fetchone()[0], self.user)

    def test_reissue_revoke_delete(self):
        """Rotation invalidates old tokens; revocation/deletion and pending-code cancellation persist."""
        device = self.consume(self.issue()).json()
        old = device['device_token']
        machine = device['machine_id']
        self.assertEqual(self.authorized(old), 422)
        code = self.manage(machine, 'reissue').json()['registration_code']
        replacement = self.consume(code).json()
        self.assertEqual(replacement['machine_id'], machine)
        self.assertEqual(self.authorized(old), 401)
        self.assertEqual(self.authorized(replacement['device_token']), 422)
        self.assertEqual(self.manage(machine, 'revoke').status_code, 200)
        self.assertEqual(self.authorized(replacement['device_token']), 401)
        pending = self.manage(machine, 'reissue').json()['registration_code']
        self.assertEqual(self.manage(machine, 'delete').status_code, 200)
        self.assertEqual(self.consume(pending).status_code, 401)
        self.assertEqual(self.manage(machine, 'reissue').status_code, 404)
        with psycopg.connect(self.dsn) as db:
            audit = db.execute('SELECT event FROM credential_audit WHERE actor_id=%s ORDER BY event_id',
                (self.user,)).fetchall()
            self.assertIn(('deleted',), audit)
            self.assertIn(('reissued',), audit)
            # Raw bearer credentials never persist in credential or audit rows.
            for table in ('device_credentials', 'registration_codes', 'credential_audit'):
                rows = str(db.execute('SELECT * FROM '+table).fetchall())
                self.assertNotIn(old, rows)
                self.assertNotIn(pending, rows)

    def test_concurrent_single_use(self):
        """Six concurrent consumers produce exactly one credential and five authentication failures."""
        code = self.issue()
        with ThreadPoolExecutor(max_workers=6) as pool:
            statuses = list(pool.map(lambda _: self.consume(code).status_code, range(6)))
        self.assertEqual(sorted(statuses), [200, 401, 401, 401, 401, 401])

    def test_validation_and_rate_limit(self):
        """Unauthenticated issuance, owner injection, oversized input and excessive issuance fail."""
        self.assertEqual(self.client.post('/v1/registration-codes').status_code, 401)
        for _ in range(10):
            self.issue()
        self.assertEqual(self.client.post('/v1/registration-codes',
            headers={'Authorization': 'Bearer '+self.session}).status_code, 429)
        self.assertEqual(self.client.post('/v1/device/registrations',
            json={'registration_code':'x', 'owner_id':str(self.user)}).status_code, 422)
        self.assertEqual(self.client.post('/v1/device/registrations', content=b'x'*4097).status_code, 413)

    def test_session_expiry_and_replacement_cancellation(self):
        """Expired/revoked user sessions fail independently; revoke cancels pending replacements."""
        device = self.consume(self.issue()).json()
        machine = device['machine_id']
        pending = self.manage(machine, 'reissue').json()['registration_code']
        self.assertEqual(self.manage(machine, 'revoke').status_code, 200)
        self.assertEqual(self.consume(pending).status_code, 401)
        with psycopg.connect(self.dsn) as db:
            db.execute("UPDATE user_sessions SET expires_at=CURRENT_TIMESTAMP-interval '1 second' WHERE user_id=%s", (self.user,))
        self.assertEqual(self.manage(machine, 'delete').status_code, 401)
        with psycopg.connect(self.dsn) as db:
            db.execute("UPDATE user_sessions SET expires_at=CURRENT_TIMESTAMP+interval '1 hour',revoked=true WHERE user_id=%s", (self.user,))
        self.assertEqual(self.manage(machine, 'delete').status_code, 401)

    def test_registered_device_ingestion_then_delete(self):
        """Registered PC moves offline -> online -> offline; replay preserves status, foreign/deleted PCs stay hidden."""
        received = datetime.now(timezone.utc)
        now = received
        self.client = TestClient(create_app(self.dsn, clock=lambda: now))
        self.addCleanup(self.client.close)
        device = self.consume(self.issue()).json()
        path = '/v1/machines/' + device['machine_id']
        owner = {'Authorization': 'Bearer '+self.session}
        other = {'Authorization': 'Bearer '+self.other}
        initial = self.client.get(path, headers=owner).json()
        self.assertFalse(initial['online'])
        self.assertIsNone(initial['last_received_at'])
        self.assertEqual(self.client.get(path, headers=other).status_code, 404)
        self.assertEqual(self.client.get('/v1/machines', headers=other).json(), {'machines': []})
        self.assertEqual(self.client.get('/v1/machines').status_code, 401)
        batch = json.loads((ROOT/'tests/fixtures/s1-03-batch.json').read_text(encoding='utf-8'))
        for sample in batch['samples']:
            sample['observed_at'] = (received-timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
        batch['machine_id'] = device['machine_id']
        batch['sensor_config_id'] = str(uuid.uuid4())
        config = next(iter(json.loads((ROOT/'contracts/fixtures/sensor-configs.json').read_text(encoding='utf-8')).values()))
        config['machine_id'] = device['machine_id']
        with psycopg.connect(self.dsn) as db:
            db.execute('INSERT INTO sensor_configs VALUES(%s,%s,%s::jsonb)',
                (batch['sensor_config_id'], device['machine_id'], json.dumps(config)))
        def send():
            return self.client.post('/v1/telemetry/batches', content=canonical(batch), headers={
                'Authorization': 'Bearer '+device['device_token'], 'Content-Type':'application/json',
                'X-Content-SHA256':digest(batch)})
        first = send()
        self.assertEqual(first.status_code, 200)
        now = received + timedelta(minutes=2)
        summary = self.client.get(path, headers=owner).json()
        self.assertTrue(summary['online'])
        self.assertEqual(datetime.fromisoformat(summary['last_received_at']), received)
        self.assertEqual(summary['cpu_temperature_c'], 52.5)
        self.assertIsNone(summary['gpu_temperature_c'])
        self.assertEqual(self.client.get('/v1/machines', headers=owner).json(), {'machines': [summary]})
        now += timedelta(microseconds=1)
        self.assertEqual(send().json(), first.json())
        self.assertEqual(self.client.get(path, headers=owner).json(), dict(summary, online=False))
        self.assertEqual(self.manage(device['machine_id'], 'delete').status_code, 200)
        self.assertEqual(self.client.get(path, headers=owner).status_code, 404)
        self.assertEqual(self.client.get('/v1/machines', headers=owner).json(), {'machines': []})
        self.assertEqual(send().status_code, 401)

    def test_registration_transaction_rollback(self):
        """Audit write failure rolls back code consumption, token and machine; retry succeeds."""
        code = self.issue()
        with psycopg.connect(self.dsn) as db:
            db.execute("CREATE FUNCTION reject_test_audit() RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN RAISE EXCEPTION 'synthetic audit failure' USING ERRCODE='P0001'; END $$")
            db.execute(f"CREATE TRIGGER reject_test_audit BEFORE INSERT ON credential_audit "
                f"FOR EACH ROW WHEN (NEW.actor_id='{self.user}'::uuid) EXECUTE FUNCTION reject_test_audit()")
        try:
            with self.assertLogs('backend.app', level='ERROR') as logs:
                failed = self.consume(code)
            self.assertEqual(failed.status_code, 503)
            self.assertNotIn(code, failed.text + ''.join(logs.output))
            with psycopg.connect(self.dsn) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM machines WHERE owner_id=%s',
                    (self.user,)).fetchone()[0], 0)
                self.assertEqual(db.execute('SELECT consumed FROM registration_codes WHERE code_sha256=%s',
                    (hashlib.sha256(code.encode()).hexdigest(),)).fetchone()[0], False)
        finally:
            with psycopg.connect(self.dsn) as db:
                db.execute('DROP TRIGGER reject_test_audit ON credential_audit')
                db.execute('DROP FUNCTION reject_test_audit()')
        self.assertEqual(self.consume(code).status_code, 200)

    def test_concurrent_delete_and_replacement(self):
        """Deletion racing replacement leaves no live credentials or reusable codes."""
        for _ in range(3):
            machine = self.consume(self.issue()).json()['machine_id']
            code = self.manage(machine, 'reissue').json()['registration_code']
            with ThreadPoolExecutor(max_workers=2) as pool:
                enrollment = pool.submit(self.consume, code)
                deletion = pool.submit(self.manage, machine, 'delete')
                response = enrollment.result(timeout=15)
                self.assertEqual(deletion.result(timeout=15).status_code, 200)
            self.assertIn(response.status_code, (200, 401))
            if response.status_code == 200:
                self.assertEqual(self.authorized(response.json()['device_token']), 401)
            self.assertEqual(self.consume(code).status_code, 401)
            with psycopg.connect(self.dsn) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM device_credentials WHERE machine_id=%s AND NOT revoked',
                    (machine,)).fetchone()[0], 0)

    def test_concurrent_rate_limit_and_reissue_limit(self):
        """Concurrent code issuance cannot exceed the limit; failed rotation preserves prior code."""
        device = self.consume(self.issue()).json()
        machine = device['machine_id']
        for _ in range(9):
            pending = self.manage(machine, 'reissue').json()['registration_code']
        self.assertEqual(self.manage(machine, 'reissue').status_code, 429)
        self.assertEqual(self.consume(pending).status_code, 200)
        def issue():
            return self.client.post('/v1/registration-codes', headers={'Authorization':'Bearer '+self.other}).status_code
        with ThreadPoolExecutor(max_workers=12) as pool:
            statuses = list(pool.map(lambda _: issue(), range(12)))
        self.assertEqual(sorted(statuses), [200]*10 + [429]*2)

    def test_expiry_while_waiting_for_owner_lock(self):
        """A code that expires while queued on a lock is rejected at actual consumption time."""
        code = self.issue()
        with psycopg.connect(self.dsn) as blocker:
            blocker.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,1))', (str(self.user),))
            with psycopg.connect(self.dsn) as db:
                db.execute("UPDATE registration_codes SET expires_at=clock_timestamp()+interval '1 second' WHERE code_sha256=%s",
                    (hashlib.sha256(code.encode()).hexdigest(),))
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(self.consume, code)
                try:
                    deadline = time.monotonic() + 5
                    waiting = False
                    while time.monotonic() < deadline:
                        with psycopg.connect(self.dsn) as db:
                            waiting = db.execute("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE wait_event='advisory' "
                                "AND query LIKE '%hashtextextended%')").fetchone()[0]
                        if waiting:
                            break
                        time.sleep(.02)
                    self.assertTrue(waiting)
                    time.sleep(1.1)
                finally:
                    blocker.commit()
                self.assertEqual(pending.result(timeout=10).status_code, 401)

    def test_session_revoked_while_waiting_for_owner_lock(self):
        """Logout while a management request waits on an owner lock prevents that mutation."""
        machine = self.consume(self.issue()).json()['machine_id']
        with psycopg.connect(self.dsn) as blocker:
            blocker.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,1))', (str(self.user),))
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(self.manage, machine, 'delete')
                try:
                    deadline = time.monotonic() + 5
                    waiting = False
                    while time.monotonic() < deadline:
                        with psycopg.connect(self.dsn) as db:
                            waiting = db.execute("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE wait_event='advisory' "
                                "AND query LIKE '%hashtextextended%')").fetchone()[0]
                        if waiting:
                            break
                        time.sleep(.02)
                    self.assertTrue(waiting)
                    with psycopg.connect(self.dsn) as db:
                        db.execute('UPDATE user_sessions SET revoked=true WHERE user_id=%s', (self.user,))
                finally:
                    blocker.commit()
                self.assertEqual(pending.result(timeout=10).status_code, 401)
        with psycopg.connect(self.dsn) as db:
            self.assertEqual(db.execute('SELECT deleted FROM machines WHERE machine_id=%s', (machine,)).fetchone()[0], False)

    def test_device_expiry_and_bad_inputs(self):
        """Expired device credentials and malformed enrollment requests fail without secret reflection."""
        device = self.consume(self.issue()).json()
        with psycopg.connect(self.dsn) as db:
            db.execute("UPDATE device_credentials SET expires_at=CURRENT_TIMESTAMP-interval '1 second' WHERE machine_id=%s",
                (device['machine_id'],))
        self.assertEqual(self.authorized(device['device_token']), 401)
        for value in (None, [], {}, {'registration_code':0}, {'registration_code':None},
                      {'registration_code':''}, {'registration_code':'x'*513}):
            response = self.client.post('/v1/device/registrations', json=value)
            self.assertEqual(response.status_code, 422)
            self.assertNotIn(device['device_token'], response.text)
        self.assertEqual(self.client.post('/v1/device/registrations', content=b'\xff').status_code, 422)
        for invalid_code in (b'{"registration_code":"\\ud800"}', b'{"registration_code":"short"}'):
            self.assertEqual(self.client.post('/v1/device/registrations', content=invalid_code).status_code, 422)
        self.assertEqual(self.manage('invalid-uuid', 'delete').status_code, 422)

    def test_documented_database_role(self):
        """Documented least-privilege API role can enroll/manage, but cannot edit sessions/audits."""
        role = 's203_api_' + uuid.uuid4().hex
        with psycopg.connect(self.base) as db:
            db.execute(f'CREATE ROLE {role} NOLOGIN')
            db.execute(f'GRANT USAGE ON SCHEMA {self.schema} TO {role}')
            for table in ('machines', 'registration_codes', 'device_credentials'):
                db.execute(f'GRANT SELECT,INSERT,UPDATE ON {self.schema}.{table} TO {role}')
            db.execute(f'GRANT SELECT ON {self.schema}.user_sessions TO {role}')
            db.execute(f'GRANT SELECT,INSERT ON {self.schema}.credential_audit TO {role}')
            db.execute(f'GRANT USAGE ON SEQUENCE {self.schema}.credential_audit_event_id_seq TO {role}')
        try:
            limited_dsn = make_conninfo(self.base, options=f'-c search_path={self.schema} -c role={role}')
            with TestClient(create_app(limited_dsn)) as client:
                headers = {'Authorization':'Bearer '+self.session}
                issued = client.post('/v1/registration-codes', headers=headers)
                self.assertEqual(issued.status_code, 200)
                enrollment = client.post('/v1/device/registrations', json={
                    'registration_code':issued.json()['registration_code']})
                self.assertEqual(enrollment.status_code, 200)
                machine = enrollment.json()['machine_id']
                self.assertEqual(client.post(f'/v1/machines/{machine}/revoke',headers=headers).status_code, 200)
                self.assertEqual(client.post(f'/v1/machines/{machine}/delete',headers=headers).status_code, 200)
            for table in ('user_sessions', 'credential_audit'):
                with psycopg.connect(limited_dsn) as db:
                    with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                        db.execute('DELETE FROM '+table)
        finally:
            with psycopg.connect(self.base) as db:
                db.execute(f'DROP OWNED BY {role}')
                # DROP OWNED runs in the current database, including this test schema.
                db.execute(f'DROP ROLE {role}')

    def test_revocation_during_body_read(self):
        """Revocation after initial authentication but before body persistence prevents writes."""
        device = self.consume(self.issue()).json()
        batch = json.loads((ROOT/'tests/fixtures/s1-03-batch.json').read_text(encoding='utf-8'))
        batch['machine_id'] = device['machine_id']
        payload = canonical(batch).encode()
        def chunks():
            yield payload[:10]
            with psycopg.connect(self.dsn) as db:
                db.execute('UPDATE device_credentials SET revoked=true WHERE machine_id=%s', (device['machine_id'],))
            yield payload[10:]
        response = self.client.post('/v1/telemetry/batches', content=chunks(), headers={
            'Authorization':'Bearer '+device['device_token'], 'Content-Type':'application/json',
            'X-Content-SHA256':digest(batch)})
        self.assertEqual(response.status_code, 401)
        with psycopg.connect(self.dsn) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM ingestion_batches WHERE machine_id=%s',
                (device['machine_id'],)).fetchone()[0], 0)

    def test_upgrade_preserves_legacy_device_credentials(self):
        """Applying 002 to populated 001 preserves legacy authentication without inventing ownership."""
        schema='test_upgrade_'+uuid.uuid4().hex
        token='synthetic-legacy-only'
        machine=uuid.uuid4()
        dsn=make_conninfo(self.base,options=f'-c search_path={schema}')
        with psycopg.connect(self.base) as db:
            db.execute(f'CREATE SCHEMA {schema}')
        try:
            with psycopg.connect(dsn) as db:
                db.execute((ROOT/'database/migrations/001_ingestion.sql').read_text(encoding='utf-8'))
                db.execute("INSERT INTO device_credentials VALUES(%s,%s,CURRENT_TIMESTAMP+interval '1 day',false)",
                    (hashlib.sha256(token.encode()).hexdigest(),machine))
                db.execute((ROOT/'database/migrations/002_registration.sql').read_text(encoding='utf-8'))
                self.assertEqual(db.execute('SELECT count(*) FROM device_credentials').fetchone()[0],1)
                self.assertEqual(db.execute('SELECT count(*) FROM machines').fetchone()[0],0)
            with TestClient(create_app(dsn)) as client:
                self.assertEqual(client.post('/v1/telemetry/batches',json={},
                    headers={'Authorization':'Bearer '+token}).status_code,422)
        finally:
            with psycopg.connect(self.base) as db:
                db.execute(f'DROP SCHEMA {schema} CASCADE')
