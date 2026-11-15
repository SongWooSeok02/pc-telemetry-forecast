"""S2-05 dashboard history adapter with snapshot-based incremental reads."""
import base64
import binascii
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

import psycopg
from fastapi import Path, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime

from .registration import owned, session

Resolution = Literal['raw', '10s', '1m', '5m', '1h']
WIDTHS = {'10s': 10, '1m': 60, '5m': 300, '1h': 3600}
MAX_PERIOD = {'raw': timedelta(hours=6), '10s': timedelta(days=1), '1m': timedelta(days=7),
              '5m': timedelta(days=30), '1h': timedelta(days=90)}
SNAPSHOT = re.compile(r'[0-9]{1,20}:[0-9]{1,20}:(?:[0-9]{1,20}(?:,[0-9]{1,20})*)?')


def pack_cursor(scope, start, end, snapshot, issued_at):
    payload = [scope, start.isoformat(), end.isoformat(), snapshot, issued_at.isoformat()]
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(',', ':')).encode()).decode()


def unpack_cursor(cursor, machine, sensor, resolution, rejected):
    try:
        stored_scope, start, end, snapshot, issued_at = json.loads(
            base64.b64decode(cursor, altchars=b'-_', validate=True)
        )
        if (not isinstance(stored_scope, list) or len(stored_scope) != 3
                or stored_scope[:2] != [str(machine), sensor] or stored_scope[2] not in MAX_PERIOD
                or (resolution is not None and resolution != stored_scope[2])
                or not SNAPSHOT.fullmatch(snapshot)):
            raise ValueError()
        dates = [datetime.fromisoformat(value) for value in (start, end, issued_at)]
        if any(value.tzinfo is None for value in dates):
            raise ValueError()
        return (*dates[:2], snapshot, dates[2], stored_scope[2])
    except (ValueError, TypeError, OverflowError, RecursionError, binascii.Error):
        raise rejected(422, 'INVALID_HISTORY_CURSOR') from None


RAW = """
    SELECT observed_at, observed_at, observed_at, received_at, value, value,
           (value IS NOT NULL)::int, quality, NULL::timestamptz, false, unit, true
    FROM measurements WHERE machine_id=%s AND sensor_id=%s AND observed_at >= %s AND observed_at < %s
    AND received_at >= %s
    AND (%s::pg_snapshot IS NULL OR observed_at >= %s OR
         NOT pg_visible_in_snapshot(changed_xid,%s::pg_snapshot))
    ORDER BY observed_at,boot_session_id,sample_seq LIMIT 100001
"""
ROLLUP = """
    WITH a AS (
        SELECT * FROM measurement_aggregates WHERE machine_id=%s AND sensor_id=%s AND bucket_seconds=%s
        AND bucket_start >= %s AND bucket_start < %s
    ), q AS (
        SELECT * FROM reaggregation_queue WHERE machine_id=%s AND sensor_id=%s AND bucket_seconds=%s
        AND bucket_start >= %s AND bucket_start < %s
    ), source AS (
        SELECT date_bin(make_interval(secs=>%s), bucket_start,'2000-01-01T00:00:00Z'::timestamptz) AS bucket,
               a.average,a.maximum,a.valid_count,a.observed_at,a.received_at,a.unit,a.quality,a.updated_at,
               q.generation IS NOT NULL AS pending,
               %s::pg_snapshot IS NULL OR bucket_start >= %s OR
                 coalesce(NOT pg_visible_in_snapshot(a.changed_xid,%s::pg_snapshot), false) OR
                 coalesce(NOT pg_visible_in_snapshot(q.changed_xid,%s::pg_snapshot), false) AS changed
        FROM a FULL JOIN q USING (bucket_start)
    )
    SELECT bucket,bucket+make_interval(secs=>%s),max(observed_at),max(received_at),
           sum(average*valid_count)/nullif(sum(valid_count),0),max(maximum),coalesce(sum(valid_count),0)::bigint,
           CASE WHEN sum(valid_count)>0 THEN 'ok'
                WHEN bool_or(quality='error') THEN 'error'
                WHEN bool_or(quality='missing') THEN 'missing'
                WHEN bool_or(quality='unsupported') THEN 'unsupported'
                ELSE 'missing' END,
           max(updated_at),bool_or(pending),max(unit),bool_or(changed)
    FROM source GROUP BY bucket ORDER BY bucket
"""


def read_snapshot(db, query, args):
    # Use the data statement's exact snapshot without putting session/owner
    # checks in REPEATABLE READ: those checks must see revocation after lock waits.
    result = db.execute(
        'SELECT pg_current_snapshot()::text, points.* FROM (SELECT 1) snapshot '
        'LEFT JOIN (' + query + ') points ON true', args,
    ).fetchall()
    return result[0][0], sorted((row[1:] for row in result if row[1] is not None), key=lambda row: row[0])


def install_dashboard_history(app, connect, rejected, clock):
    @app.get('/v1/dashboard/machines/{machine_id}/sensors/{sensor_id:path}/history')
    def history(machine_id: UUID, request: Request, sensor_id: str = Path(min_length=1, max_length=256),
                resolution: Resolution | None = Query(None),
                start: AwareDatetime | None = Query(None, alias='from'),
                end: AwareDatetime | None = Query(None, alias='to'),
                cursor: str | None = Query(None, max_length=8192)):
        now = clock().astimezone(timezone.utc)
        snapshot = old_end = issued = None
        if cursor is not None:
            start, end, snapshot, issued, resolution = unpack_cursor(
                cursor, machine_id, sensor_id, resolution, rejected)
            try:
                old_end = end
                elapsed = max(now-issued, timedelta())
                start, end = start+elapsed, end+elapsed
            except OverflowError:
                raise rejected(422, 'INVALID_HISTORY_CURSOR') from None
        if (resolution is None or start is None or end is None
                or not timedelta() < end-start <= MAX_PERIOD[resolution]):
            raise rejected(422, 'INVALID_TIME_RANGE')
        with connect() as db:
            db.execute("SET LOCAL statement_timeout = '10s'")
            db.execute("SET LOCAL lock_timeout = '5s'")
            db.execute("SET LOCAL TIME ZONE 'UTC'")
            user = session(rejected, db, request)
            owned(rejected, db, user, machine_id)
            try:
                if resolution == 'raw':
                    next_snapshot, rows = read_snapshot(
                        db, RAW, (machine_id, sensor_id, start, end, now-timedelta(days=7),
                                  snapshot, old_end, snapshot))
                else:
                    width = WIDTHS[resolution]
                    base_width = min(width, 60)
                    retained = now-timedelta(days=30 if width == 10 else 90)
                    filters = (machine_id, sensor_id, base_width, max(start, retained), end)
                    next_snapshot, rows = read_snapshot(
                        db, ROLLUP, (*filters, *filters, width, snapshot, old_end, snapshot, snapshot, width))
            except psycopg.DataError:
                if snapshot is None:
                    raise
                raise rejected(422, 'INVALID_HISTORY_CURSOR') from None
            if resolution == 'raw' and len(rows) > 100000:
                raise rejected(422, 'HISTORY_RANGE_TOO_DENSE')
            unit = next((row[10] for row in reversed(rows) if row[10] is not None), None)
            if unit is None:
                known = db.execute(
                    "SELECT config->'sensors'->%s::text->>'unit' FROM sensor_configs "
                    "WHERE machine_id=%s AND (config->'sensors') ? %s ORDER BY sensor_config_id LIMIT 1",
                    (sensor_id, machine_id, sensor_id),
                ).fetchone()
                if known is None:
                    raise rejected(404, 'SENSOR_NOT_FOUND')
                unit = known[0]
        points = []
        for bucket, bucket_end, observed, received, value, maximum, count, quality, updated, pending, _, changed in rows:
            aged = (resolution != 'raw' and issued is not None and
                    issued-timedelta(hours=24) < bucket_end <= now-timedelta(hours=24))
            if changed or aged:
                points.append(dict(bucketStart=bucket, bucketEnd=bucket_end, lastObservedAt=observed,
                                   receivedAt=received, value=value, max=maximum, sampleCount=count, quality=quality,
                                   aggregationUpdatedAt=updated, aggregationPending=pending,
                                   provisional=resolution != 'raw' and (
                                       pending or bucket_end > now-timedelta(hours=24))))
        scope = [str(machine_id), sensor_id, resolution]
        payload = dict(machineId=machine_id, sensorId=sensor_id, resolution=resolution, unit=unit,
                       points=points, nextCursor=pack_cursor(scope, start, end, next_snapshot, now),
                       aggregationUpdatedAt=max((row[8] for row in rows if row[8] is not None), default=None),
                       aggregationPending=any(row[9] for row in rows))
        return JSONResponse(jsonable_encoder(payload), headers={'Cache-Control': 'no-store'})
