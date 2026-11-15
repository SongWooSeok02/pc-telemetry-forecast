"""Browser session boundary. The trusted login adapter issues the underlying session."""
import hashlib
import hmac

from fastapi import Request, Response
from fastapi.responses import JSONResponse


COOKIE = '__Host-pct_session'
SCOPE = 'x-pct-session'
NO_STORE = {'Cache-Control': 'no-store'}


def credential(request):
    # Never fall back to cookies when an explicit credential was supplied.
    header = request.headers.get('authorization')
    if header is not None:
        return (header[7:] if header.startswith('Bearer ') else ''), False
    return request.cookies.get(COOKIE, ''), True


def scope(token):
    # A separate digest, never accepted as an authentication credential.
    return hashlib.sha256(('browser-session:' + token).encode()).hexdigest()


def lookup(db, request, *, bind=True):
    token, browser = credential(request)
    if not 0 < len(token) <= 512:
        return None, 'USER_UNAUTHENTICATED'
    if browser and bind:
        supplied = request.headers.get(SCOPE, '')
        if not supplied.isascii() or not hmac.compare_digest(supplied, scope(token)):
            return None, 'USER_UNAUTHENTICATED'
    digest = hashlib.sha256(token.encode()).hexdigest()
    row = db.execute(
        'SELECT user_id, revoked, EXTRACT(EPOCH FROM expires_at - clock_timestamp()) FROM user_sessions WHERE token_sha256=%s',
        (digest,),
    ).fetchone()
    if not row or row[1]:
        return None, 'USER_UNAUTHENTICATED'
    if row[2] <= 0:
        return None, 'USER_SESSION_EXPIRED'
    return (row[0], digest, int(row[2] * 1000)), None


def require_session(rejected, db, request, *, bind=True):
    principal, error = lookup(db, request, bind=bind)
    if error:
        raise rejected(401, error)
    db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,1))', (str(principal[0]),))
    # A lock wait can overlap expiry or logout.
    active, error = lookup(db, request, bind=bind)
    if error or active[:2] != principal[:2]:
        raise rejected(401, error or 'USER_UNAUTHENTICATED')
    return active


def install_user_session(app, connect, rejected):
    @app.get('/v1/user/session')
    def check(request: Request):
        with connect() as db:
            _, _, expires_in_ms = require_session(rejected, db, request, bind=False)
        token, browser = credential(request)
        return JSONResponse({'scope': scope(token) if browser else None, 'expires_in_ms': expires_in_ms}, headers=NO_STORE)

    @app.post('/v1/user/session/logout', status_code=204)
    def logout(request: Request):
        with connect() as db:
            _, digest, _ = require_session(rejected, db, request)
            db.execute('UPDATE user_sessions SET revoked=true WHERE token_sha256=%s', (digest,))
        # A delayed logout response must not clear a newer login cookie in another tab.
        # The revoked value is unusable and the adapter replaces it on the next login.
        return Response(status_code=204, headers=NO_STORE)
