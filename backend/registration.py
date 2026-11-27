"""Device lifecycle. Opaque user sessions are supplied by the trusted login adapter."""
import hashlib
import re
import secrets
import uuid
from datetime import timedelta, timezone
from functools import partial

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


ONLINE_WINDOW = timedelta(minutes=2)


def session(rejected, db, request):
    header = request.headers.get('authorization', '')
    row = None
    if header.startswith('Bearer ') and 0 < len(header[7:]) <= 512:
        row = db.execute('SELECT user_id FROM user_sessions WHERE token_sha256=%s '
            'AND NOT revoked AND expires_at>clock_timestamp()', (sha(header[7:]),)).fetchone()
    if row is None:
        raise rejected(401, 'USER_UNAUTHENTICATED')
    # Serialize owner operations: prevents issuing a replacement code racing deletion.
    db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,1))', (str(row[0]),))
    # Lock waits must not extend a user's session lifetime or bypass logout.
    active = db.execute('SELECT user_id FROM user_sessions WHERE token_sha256=%s '
        'AND NOT revoked AND expires_at>clock_timestamp()', (sha(header[7:]),)).fetchone()
    if active is None:
        raise rejected(401, 'USER_UNAUTHENTICATED')
    return row[0]


def owned(rejected, db, user, machine):
    row = db.execute('SELECT machine_id FROM machines WHERE machine_id=%s '
        'AND owner_id=%s AND NOT deleted FOR UPDATE', (machine, user)).fetchone()
    if row is None:
        raise rejected(404, 'MACHINE_NOT_FOUND')


def install(app, connect, rejected, clock):
    require_session = partial(session, rejected)
    require_owner = partial(owned, rejected)

    async def body(request, fields):
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 4096:
                raise rejected(413, 'PAYLOAD_TOO_LARGE')
        try:
            import json
            value = json.loads(raw)
            if not isinstance(value, dict) or set(value) != set(fields):
                raise ValueError()
            if any(not isinstance(v, str) or not 1 <= len(v) <= 512 for v in value.values()):
                raise ValueError()
            return value
        except (ValueError, UnicodeError, RecursionError):
            raise rejected(422, 'VALIDATION_ERROR')

    def audit(db, user, machine, event):
        db.execute('INSERT INTO credential_audit(actor_id,machine_id,event) VALUES(%s,%s,%s)',
                   (user, machine, event))

    def result(value):
        return JSONResponse(value, headers={'Cache-Control': 'no-store', 'Pragma': 'no-cache'})

    def limit(db, user):
        count = db.execute("SELECT count(*) FROM credential_audit WHERE actor_id=%s "
            "AND event='code_issued' AND occurred_at>CURRENT_TIMESTAMP-interval '1 hour'", (user,)).fetchone()[0]
        if count >= 10:
            raise rejected(429, 'REGISTRATION_RATE_LIMITED')

    def summaries(request, machine=None):
        with connect() as db:
            user = require_session(db, request)
            query = """
                SELECT m.machine_id, latest.received_at, temps.cpu, temps.gpu
                FROM machines m
                LEFT JOIN LATERAL (
                    SELECT batch_id, received_at FROM ingestion_batches
                    WHERE machine_id=m.machine_id
                    ORDER BY received_at DESC, batch_id DESC LIMIT 1
                ) latest ON true
                LEFT JOIN LATERAL (
                    SELECT max(r.value) FILTER (WHERE sensor.scope='cpu_package') AS cpu,
                           max(r.value) FILTER (WHERE sensor.scope='gpu_core') AS gpu
                    FROM measurements r
                    JOIN sensor_configs c USING (sensor_config_id)
                    CROSS JOIN LATERAL (
                        SELECT c.config->'sensors'->r.sensor_id->>'scope' AS scope
                    ) sensor
                    WHERE r.machine_id=m.machine_id AND r.received_at=latest.received_at
                      AND r.batch_id=latest.batch_id AND r.unit='Cel'
                ) temps ON true
                WHERE m.owner_id=%s AND NOT m.deleted
            """
            params = (user,)
            if machine is not None:
                query += ' AND m.machine_id=%s'
                params += (machine,)
            rows = db.execute(query + ' ORDER BY m.machine_id', params).fetchall()
        cutoff = clock().astimezone(timezone.utc) - ONLINE_WINDOW
        return [
            dict(machine_id=str(key), name=str(key), online=received is not None and received >= cutoff,
                 last_received_at=received.astimezone(timezone.utc).isoformat() if received else None,
                 cpu_temperature_c=float(cpu) if cpu is not None else None,
                 gpu_temperature_c=float(gpu) if gpu is not None else None)
            for key, received, cpu, gpu in rows
        ]

    @app.get('/v1/machines')
    def list_machines(request: Request):
        return result({'machines': summaries(request)})

    @app.get('/v1/machines/{machine_id}')
    def get_machine(machine_id: uuid.UUID, request: Request):
        rows = summaries(request, machine_id)
        if not rows:
            raise rejected(404, 'MACHINE_NOT_FOUND')
        return result(rows[0])

    @app.post('/v1/registration-codes')
    def issue(request: Request):
        with connect() as db:
            user = require_session(db, request)
            # Bound outstanding codes and issuance per owner; a proxy must also rate limit failures.
            limit(db, user)
            code = secrets.token_urlsafe(24)
            expires = db.execute("INSERT INTO registration_codes VALUES(%s,%s,NULL,"
                "clock_timestamp()+interval '10 minutes',false) RETURNING expires_at", (sha(code), user)).fetchone()[0]
            audit(db, user, None, 'code_issued')
        return result({'registration_code': code, 'expires_at': expires.isoformat()})

    @app.post('/v1/device/registrations')
    async def consume(request: Request):
        value = await body(request, ['registration_code'])
        if not re.fullmatch(r'[A-Za-z0-9_-]{32}', value['registration_code']):
            raise rejected(422, 'VALIDATION_ERROR')
        code_hash = sha(value['registration_code'])
        def work():
            with connect() as db:
                # Capability possession authorizes enrollment; owner comes only from the DB.
                owner = db.execute('SELECT owner_id FROM registration_codes WHERE code_sha256=%s',
                    (code_hash,)).fetchone()
                if owner is None:
                    raise rejected(401, 'REGISTRATION_INVALID')
                db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,1))', (str(owner[0]),))
                row = db.execute('SELECT owner_id,machine_id FROM registration_codes WHERE code_sha256=%s '
                    'AND NOT consumed AND expires_at>clock_timestamp() FOR UPDATE',
                    (code_hash,)).fetchone()
                if row is None:
                    raise rejected(401, 'REGISTRATION_INVALID')
                user, machine = row
                if machine:
                    require_owner(db, user, machine)
                    db.execute('UPDATE device_credentials SET revoked=true WHERE machine_id=%s', (machine,))
                    event = 'reissued'
                else:
                    machine = uuid.uuid4()
                    db.execute('INSERT INTO machines VALUES(%s,%s,false)', (machine, user))
                    event = 'registered'
                token = secrets.token_urlsafe(48)
                expires = db.execute("INSERT INTO device_credentials VALUES(%s,%s,"
                    "clock_timestamp()+interval '90 days',false) RETURNING expires_at", (sha(token), machine)).fetchone()[0]
                db.execute('UPDATE registration_codes SET consumed=true WHERE code_sha256=%s',
                           (code_hash,))
                audit(db, user, machine, event)
            return result({'machine_id': str(machine), 'device_token': token, 'expires_at': expires.isoformat()})
        return await run_in_threadpool(work)

    @app.post('/v1/machines/{machine_id}/{action}')
    def manage(machine_id: uuid.UUID, action: str, request: Request):
        if action not in ('reissue', 'revoke', 'delete'):
            raise rejected(404, 'NOT_FOUND')
        with connect() as db:
            user = require_session(db, request)
            require_owner(db, user, machine_id)
            # Invalidate pending replacements for every lifecycle action.
            db.execute('UPDATE registration_codes SET consumed=true WHERE machine_id=%s', (machine_id,))
            if action == 'reissue':
                limit(db, user)
                code = secrets.token_urlsafe(24)
                expires = db.execute("INSERT INTO registration_codes VALUES(%s,%s,%s,"
                    "clock_timestamp()+interval '10 minutes',false) RETURNING expires_at",
                    (sha(code), user, machine_id)).fetchone()[0]
                audit(db, user, machine_id, 'code_issued')
                response = {'registration_code': code, 'expires_at': expires.isoformat()}
            else:
                db.execute('UPDATE device_credentials SET revoked=true WHERE machine_id=%s', (machine_id,))
                if action == 'delete':
                    db.execute('UPDATE machines SET deleted=true WHERE machine_id=%s', (machine_id,))
                audit(db, user, machine_id, 'deleted' if action == 'delete' else 'revoked')
                response = {'status': action}
        return result(response)
