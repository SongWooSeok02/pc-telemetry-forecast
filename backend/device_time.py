"""Authenticated S1-01 time exchange."""
import hashlib
import os
import time
import uuid
from datetime import timezone

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .contract import decode, fields


def install(app, connect, rejected, clock, clock_precision):
    @app.post('/v1/device/time')
    async def device_time(request: Request):
        received = clock().astimezone(timezone.utc)
        authorization = request.headers.get('authorization', '')
        if not authorization.startswith('Bearer ') or not authorization[7:]:
            raise rejected(401, 'DEVICE_UNAUTHENTICATED')

        def authenticate():
            with connect() as db:
                return db.execute(
                    'SELECT 1 FROM device_credentials WHERE token_sha256=%s '
                    'AND NOT revoked AND expires_at>clock_timestamp()',
                    (hashlib.sha256(authorization[7:].encode()).hexdigest(),)
                ).fetchone()

        if not await run_in_threadpool(authenticate):
            raise rejected(401, 'DEVICE_UNAUTHENTICATED')

        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 1024:
                raise rejected(413, 'PAYLOAD_TOO_LARGE')
            raw.extend(chunk)
        try:
            body = decode(raw.decode('utf-8'))
        except (ValueError, ArithmeticError, RecursionError):
            raise rejected(400, 'INVALID_JSON')
        try:
            request_id = body['request_id']
            if str(uuid.UUID(request_id)) != request_id:
                raise ValueError('request id')
        except (ValueError, AttributeError, KeyError, TypeError):
            raise rejected(422, 'VALIDATION_ERROR')
        try:
            fields(body, ('schema_version', 'request_id'))
            if body['schema_version'] != '0.1.0':
                raise ValueError('version')
        except ValueError:
            raise rejected(422, 'VALIDATION_ERROR', request_id)

        precision = clock_precision()
        sent = clock().astimezone(timezone.utc)
        if type(precision) is not int or not 0 < precision <= 2147483647 or sent < received:
            raise rejected(503, 'CLOCK_UNTRUSTED', request_id)
        return JSONResponse({
            'schema_version': '0.1.0',
            'request_id': request_id,
            'server_received_at': received.strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
            'server_sent_at': sent.strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
            'precision_us': precision,
            'clock_status': 'synchronized',
        }, headers={'Cache-Control': 'no-store'})


def monitored_precision():
    """Read the precision published by the host clock monitor."""
    path = os.getenv('CLOCK_PRECISION_FILE')
    if not path:
        return None
    try:
        with open(path, encoding='ascii') as status:
            age = time.time() - os.fstat(status.fileno()).st_mtime
            if 0 <= age <= 30:
                return int(status.read(32))
    except (OSError, ValueError):
        return None
    return None
