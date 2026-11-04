"""FastAPI ingestion adapter. No synthetic credentials are loaded at startup."""
import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timezone

import psycopg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from .contract import canonical, decode, digest, validate

MAX_BODY = 1048576
log = logging.getLogger(__name__)


class Rejected(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code


def create_app(dsn=None, *, clock=None, before_commit=None):
    app = FastAPI()
    clock = clock or (lambda: datetime.now(timezone.utc))

    def connect():
        return psycopg.connect(dsn or os.environ['DATABASE_URL'], connect_timeout=5)

    from .registration import install
    install(app, connect, Rejected)

    @app.exception_handler(Rejected)
    async def rejected(request, exc):
        headers = {'WWW-Authenticate': 'Bearer'} if exc.status == 401 else {}
        if exc.status == 503:
            headers['Retry-After'] = '5'
        return JSONResponse({'error': {'code': exc.code, 'message': exc.code,
            'request_id': str(uuid.uuid4()),
            'retryable': exc.code in ('SERVICE_UNAVAILABLE', 'SENSOR_CONFIG_PENDING'),
            'details': []}}, status_code=exc.status, headers=headers)

    @app.exception_handler(psycopg.Error)
    async def database_error(request, exc):
        # SQL text/values may contain telemetry or secrets; do not log the exception.
        log.error('ingestion_database_error sqlstate=%s', exc.sqlstate)
        return await rejected(request, Rejected(503, 'SERVICE_UNAVAILABLE'))

    @app.post('/v1/telemetry/batches')
    async def receive(request: Request):
        received = clock().astimezone(timezone.utc)
        # Authenticate before reading a potentially large body; never store raw tokens.
        authorization = request.headers.get('authorization', '')
        if not authorization.startswith('Bearer ') or not authorization[7:]:
            raise Rejected(401, 'DEVICE_UNAUTHENTICATED')
        def authenticate():
            with connect() as db:
                row = db.execute('SELECT machine_id FROM device_credentials WHERE token_sha256=%s '
                    'AND NOT revoked AND expires_at>CURRENT_TIMESTAMP',
                    (hashlib.sha256(authorization[7:].encode()).hexdigest(),)).fetchone()
            return row
        row = await run_in_threadpool(authenticate)
        if row is None:
            raise Rejected(401, 'DEVICE_UNAUTHENTICATED')
        machine = str(row[0])
        if request.headers.get('content-encoding', 'identity') != 'identity':
            raise Rejected(415, 'UNSUPPORTED_ENCODING')
        if request.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json':
            raise Rejected(415, 'UNSUPPORTED_MEDIA_TYPE')
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > MAX_BODY:
                raise Rejected(413, 'PAYLOAD_TOO_LARGE')
        try:
            body = decode(raw.decode('utf-8'))
        except (ValueError, UnicodeError, ArithmeticError, RecursionError):
            raise Rejected(400, 'INVALID_JSON')
        if not isinstance(body, dict) or not isinstance(body.get('machine_id'), str):
            raise Rejected(422, 'VALIDATION_ERROR')
        if body['machine_id'] != machine:
            raise Rejected(403, 'DEVICE_FORBIDDEN')
        try:
            content = canonical(body)
            sha = digest(body)
            # Guard UUID binds before SQL (do not map input errors to 503).
            for key in ('batch_id', 'sensor_config_id'):
                if str(uuid.UUID(body[key])) != body[key]:
                    raise ValueError('uuid')
        except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError, RecursionError):
            raise Rejected(422, 'VALIDATION_ERROR')
        if not request.headers.get('x-content-sha256'):
            raise Rejected(422, 'VALIDATION_ERROR')
        if request.headers['x-content-sha256'] != sha:
            raise Rejected(422, 'HASH_MISMATCH')
        def persist():
            with connect() as db:
                db.execute("SET LOCAL statement_timeout = '10s'")
                db.execute("SET LOCAL lock_timeout = '5s'")
                # A transaction-scoped device lock orders concurrent requests, including
                # overlapping batches. Constraints remain authoritative across partitions.
                db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', (machine,))
                # Recheck inside the write transaction: a token may have been revoked
                # while this request was validating its body.
                credential = db.execute('SELECT machine_id FROM device_credentials WHERE token_sha256=%s '
                    'AND NOT revoked AND expires_at>CURRENT_TIMESTAMP FOR SHARE',
                    (hashlib.sha256(authorization[7:].encode()).hexdigest(),)).fetchone()
                if credential is None:
                    raise Rejected(401, 'DEVICE_UNAUTHENTICATED')
                prior = db.execute('SELECT canonical_body,response FROM ingestion_batches '
                    'WHERE machine_id=%s AND batch_id=%s', (machine, body['batch_id'])).fetchone()
                if prior:
                    if prior[0] != content:
                        raise Rejected(409, 'BATCH_CONFLICT')
                    return JSONResponse(prior[1])
                config_row = db.execute('SELECT machine_id,config FROM sensor_configs WHERE sensor_config_id=%s',
                    (body['sensor_config_id'],)).fetchone()
                if config_row is None:
                    raise Rejected(409, 'SENSOR_CONFIG_PENDING')
                if str(config_row[0]) != machine:
                    raise Rejected(403, 'DEVICE_FORBIDDEN')
                try:
                    validate(body, {body['sensor_config_id']: config_row[1]})
                except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError):
                    raise Rejected(422, 'VALIDATION_ERROR')
                response = dict(schema_version='0.1.0', batch_id=body['batch_id'],
                    accepted_samples=len(body['samples']), received_at=received.strftime('%Y-%m-%dT%H:%M:%S.%fZ'))
                db.execute('INSERT INTO ingestion_batches VALUES(%s,%s,%s,%s,%s::jsonb,%s)',
                    (machine, body['batch_id'], sha, content, json.dumps(response), received))
                try:
                    for sample in body['samples']:
                        key = (machine, body['boot_session_id'], int(sample['sample_seq']))
                        db.execute('INSERT INTO ingestion_samples VALUES(%s,%s,%s,%s)', (*key, body['batch_id']))
                        for r in sample['readings']:
                            db.execute('INSERT INTO measurements '
                                '(received_at,machine_id,boot_session_id,sample_seq,batch_id,sensor_config_id,'
                                'sensor_id,observed_at,monotonic_ms,unit,quality,value,reason) '
                                'VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                                (received, *key, body['batch_id'], body['sensor_config_id'], r['sensor_id'],
                                 sample['observed_at'], int(sample['monotonic_ms']), r['unit'], r['quality'],
                                 r['value'], r.get('reason')))
                            for seconds in (10, 60):
                                db.execute('INSERT INTO reaggregation_queue '
                                    '(machine_id,sensor_id,bucket_seconds,bucket_start) '
                                    "VALUES(%s,%s,%s,date_bin(%s::interval,%s::timestamptz,'2000-01-01+00'::timestamptz)) "
                                    'ON CONFLICT(machine_id,sensor_id,bucket_seconds,bucket_start) '
                                    'DO UPDATE SET generation=reaggregation_queue.generation+1',
                                    (machine, r['sensor_id'], seconds, f'{seconds} seconds', sample['observed_at']))
                except psycopg.errors.UniqueViolation:
                    raise Rejected(409, 'SAMPLE_CONFLICT')
                if before_commit:
                    before_commit(db)  # Test seam; never configurable over HTTP.
            # Context manager has committed before success is returned.
            return JSONResponse(response)
        return await run_in_threadpool(persist)

    @app.get('/health/partitions')
    def partition_health():
        with connect() as db:
            # Check actual attachment and UTC bounds, rather than table names alone.
            today = db.execute("SELECT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::date").fetchone()[0]
            db.execute("SET LOCAL TIME ZONE 'UTC'")
            bounds = db.execute("SELECT pg_get_expr(c.relpartbound,c.oid) FROM pg_inherits i "
                "JOIN pg_class c ON c.oid=i.inhrelid WHERE i.inhparent='measurements'::regclass").fetchall()
        from datetime import timedelta
        actual = {r[0] for r in bounds}
        missing = []
        for offset in range(4):
            day = today + timedelta(days=offset)
            end = day + timedelta(days=1)
            expected = f"FOR VALUES FROM ('{day} 00:00:00+00') TO ('{end} 00:00:00+00')"
            if expected not in actual:
                missing.append(str(day))
        return JSONResponse({'ready': not missing, 'missing_utc_days': missing}, status_code=503 if missing else 200)

    return app


app = create_app()
