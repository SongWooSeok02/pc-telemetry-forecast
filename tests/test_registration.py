"""Real PostgreSQL lifecycle tests; all credentials here are synthetic."""
import hashlib
import json
import os
from pathlib import Path
import unittest
import uuid
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
        """A newly registered credential stores a real batch; deletion rejects exact replay."""
        device = self.consume(self.issue()).json()
        batch = json.loads((ROOT/'tests/fixtures/s1-03-batch.json').read_text(encoding='utf-8'))
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
        self.assertEqual(send().status_code, 200)
        self.assertEqual(self.manage(device['machine_id'], 'delete').status_code, 200)
        self.assertEqual(send().status_code, 401)
