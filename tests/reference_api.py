"""Executable HTTP contract adapter, SQLite transactions; not the production backend.

Only synthetic credentials and catalog are injected by tests. Bind loopback only.
"""
import json
import sqlite3
import threading
import uuid
from contextlib import closing
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from verify_contract import canonical, decode, digest, fields, validate


def utc():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def error(status, code, path='', request_id=None):
    return status, {'error': {'code': code, 'message': code,
        'request_id': request_id or str(uuid.uuid4()),
        'retryable': code in ('SENSOR_CONFIG_PENDING', 'SERVICE_UNAVAILABLE', 'CLOCK_UNTRUSTED', 'REQUEST_TIMEOUT'),
        'details': [{'path': path, 'rule': code}] if path else []}}


class Store:
    def __init__(self, path, configs, token, machine):
        self.path, self.configs, self.token, self.machine = path, configs, token, machine
        self.fail_before_commit = False
        self.clock_synced = True  # Test injection, never evidence of real host synchronization.
        self.lock = threading.Lock()
        with closing(sqlite3.connect(path)) as db:
            db.executescript('''
              CREATE TABLE batches(machine TEXT, id TEXT, body TEXT, sha TEXT, response TEXT,
                  PRIMARY KEY(machine,id));
              CREATE TABLE samples(machine TEXT, boot TEXT, seq INTEGER,
                  PRIMARY KEY(machine,boot,seq));
              CREATE TABLE measurements(machine TEXT, boot TEXT, seq INTEGER, sensor TEXT,
                  body TEXT, received_at TEXT, PRIMARY KEY(machine,boot,seq,sensor));
              CREATE TABLE reaggregate(machine TEXT, boot TEXT, seq INTEGER,
                  PRIMARY KEY(machine,boot,seq));
            ''')

    def dispatch(self, path, raw, headers):
        received = utc()
        if headers.get('Authorization') != 'Bearer '+self.token:
            return error(401, 'DEVICE_UNAUTHENTICATED')
        if headers.get('Content-Encoding', 'identity') != 'identity':
            return error(415, 'UNSUPPORTED_ENCODING')
        if len(raw) > 1048576:
            return error(413, 'PAYLOAD_TOO_LARGE')
        try:
            body = decode(raw.decode('utf-8'))
        except (ValueError, UnicodeError, ArithmeticError):
            return error(400, 'INVALID_JSON')
        if path == '/v1/device/time':
            try:
                fields(body, ('schema_version', 'request_id'))
                rid = body['request_id']
                if not isinstance(rid, str) or str(uuid.UUID(rid)) != rid or body['schema_version'] != '0.1.0':
                    raise ValueError('time request')
            except (ValueError, TypeError, AttributeError):
                return error(422, 'VALIDATION_ERROR')
            if not self.clock_synced:
                return error(503, 'CLOCK_UNTRUSTED', request_id=rid)
            sent = utc()
            if sent < received:
                return error(503, 'CLOCK_UNTRUSTED', request_id=rid)
            return 200, dict(schema_version='0.1.0', request_id=rid,
                server_received_at=received, server_sent_at=sent,
                precision_us=1000, clock_status='synchronized')
        if path != '/v1/telemetry/batches':
            return error(404, 'NOT_FOUND')
        if not isinstance(body, dict):
            return error(422, 'VALIDATION_ERROR')
        if not isinstance(body.get('machine_id'), str):
            return error(422, 'VALIDATION_ERROR')
        if body.get('machine_id') != self.machine:
            return error(403, 'DEVICE_FORBIDDEN')
        if not isinstance(body.get('batch_id'), str) or not isinstance(body.get('sensor_config_id'), str):
            return error(422, 'VALIDATION_ERROR')
        try:
            computed = digest(body)
        except (ValueError, TypeError, ArithmeticError):
            return error(422, 'VALIDATION_ERROR')
        if not headers.get('X-Content-SHA256'):
            return error(422, 'VALIDATION_ERROR', '/headers/X-Content-SHA256')
        if headers['X-Content-SHA256'] != computed:
            return error(422, 'HASH_MISMATCH')
        # SQLite serializes writers. Unique keys remain the final constraint.
        with self.lock, closing(sqlite3.connect(self.path)) as db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT body,response FROM batches WHERE machine=? AND id=?',
                (self.machine, body.get('batch_id'))).fetchone()
            if prior:
                return (200, json.loads(prior[1])) if prior[0] == canonical(body) else error(409, 'BATCH_CONFLICT')
            config = self.configs.get(body.get('sensor_config_id'))
            if config is None:
                return error(409, 'SENSOR_CONFIG_PENDING')
            if config['machine_id'] != self.machine:
                return error(403, 'DEVICE_FORBIDDEN')
            try:
                validate(body, self.configs)
            except (ValueError, TypeError, KeyError, ArithmeticError):
                return error(422, 'VALIDATION_ERROR')
            response = dict(schema_version='0.1.0', batch_id=body['batch_id'],
                            accepted_samples=len(body['samples']), received_at=received)
            try:
                db.execute('INSERT INTO batches VALUES(?,?,?,?,?)',
                    (self.machine, body['batch_id'], canonical(body), computed, json.dumps(response)))
                for sample in body['samples']:
                    key = (self.machine, body['boot_session_id'], int(sample['sample_seq']))
                    db.execute('INSERT INTO samples VALUES(?,?,?)', key)
                    for reading in sample['readings']:
                        db.execute('INSERT INTO measurements VALUES(?,?,?,?,?,?)',
                            (*key, reading['sensor_id'], canonical(reading), received))
                    db.execute('INSERT INTO reaggregate VALUES(?,?,?)', key)
                if self.fail_before_commit:
                    raise sqlite3.OperationalError('injected commit failure')
                db.commit()
                return 200, response
            except sqlite3.IntegrityError:
                db.rollback()
                return error(409, 'SAMPLE_CONFLICT')
            except sqlite3.OperationalError:
                db.rollback()
                return error(503, 'SERVICE_UNAVAILABLE')

    def counts(self):
        with closing(sqlite3.connect(self.path)) as db:
            return {t: db.execute('SELECT count(*) FROM '+t).fetchone()[0]
                    for t in ('batches', 'samples', 'measurements', 'reaggregate')}


def server(store):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                size = int(self.headers.get('Content-Length', '-1'))
            except ValueError:
                size = -1
            if size < 0 or size > 1048576:
                status, body = error(413, 'PAYLOAD_TOO_LARGE')
            else:
                status, body = store.dispatch(self.path, self.rfile.read(size), self.headers)
            encoded = json.dumps(body).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(encoded)))
            if status == 401:
                self.send_header('WWW-Authenticate', 'Bearer')
            if status == 503:
                self.send_header('Retry-After', '5')
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *_):
            pass
    return ThreadingHTTPServer(('127.0.0.1', 0), Handler)
