"""Owner-scoped time series for charts and prediction consumers."""
from datetime import timedelta
from uuid import UUID

from fastapi import Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from psycopg.rows import dict_row
from pydantic import AwareDatetime

from .registration import owned, session


def install_history(app, connect, rejected, clock):
    def read(request, machine, start, end, limit, sql, args, transform=None):
        with connect() as db:
            db.execute("SET LOCAL statement_timeout = '10s'")
            db.execute("SET LOCAL lock_timeout = '5s'")
            db.execute("SET LOCAL TIME ZONE 'UTC'")
            user = session(rejected, db, request)
            owned(rejected, db, user, machine)
            if start >= end or end - start > timedelta(days=90):
                raise rejected(422, 'INVALID_TIME_RANGE')
            with db.cursor(row_factory=dict_row) as cur:
                rows = cur.execute(sql, (*args, limit + 1)).fetchall()
        page = rows[:limit]
        if transform:
            for row in page:
                transform(row)
        return JSONResponse(jsonable_encoder({'items': page, 'truncated': len(rows) > limit}),
                            headers={'Cache-Control': 'no-store'})

    @app.get('/v1/machines/{machine_id}/measurements')
    def measurements(machine_id: UUID, request: Request, sensor_id: str,
                     start: AwareDatetime, end: AwareDatetime, limit: int = Query(1000, ge=1, le=5000)):
        return read(request, machine_id, start, end, limit, '''
            SELECT observed_at,received_at,quality,value,reason,unit,boot_session_id,sample_seq
            FROM measurements WHERE machine_id=%s AND sensor_id=%s
            AND observed_at >= %s AND observed_at < %s AND received_at >= %s
            ORDER BY observed_at,boot_session_id,sample_seq LIMIT %s
        ''', (machine_id, sensor_id, start, end, clock() - timedelta(days=7)))

    @app.get('/v1/machines/{machine_id}/aggregates')
    def aggregates(machine_id: UUID, request: Request, sensor_id: str,
                   start: AwareDatetime, end: AwareDatetime, bucket_seconds: int = Query(60),
                   limit: int = Query(1000, ge=1, le=5000)):
        if bucket_seconds not in (10, 60):
            raise rejected(422, 'INVALID_BUCKET_WIDTH')
        now = clock()
        start_retained = max(start, now - timedelta(days=30 if bucket_seconds == 10 else 90))
        filters = (machine_id, sensor_id, bucket_seconds, start_retained, end)

        def status(row):
            row['provisional'] = row['pending'] or (
                row['bucket_start'] + timedelta(seconds=bucket_seconds) > now - timedelta(hours=24)
            )

        return read(request, machine_id, start, end, limit, '''
            SELECT bucket_start,a.average,a.maximum,a.valid_count,a.updated_at,
                   q.bucket_start IS NOT NULL AS pending
            FROM (SELECT bucket_start,average,maximum,valid_count,updated_at FROM measurement_aggregates
                  WHERE machine_id=%s AND sensor_id=%s AND bucket_seconds=%s
                  AND bucket_start >= %s AND bucket_start < %s) a
            FULL JOIN (SELECT bucket_start FROM reaggregation_queue
                       WHERE machine_id=%s AND sensor_id=%s AND bucket_seconds=%s
                       AND bucket_start >= %s AND bucket_start < %s) q USING (bucket_start)
            ORDER BY bucket_start LIMIT %s
        ''', (*filters, *filters), status)

    @app.get('/v1/machines/{machine_id}/predictions')
    def predictions(machine_id: UUID, request: Request, sensor_id: str,
                    start: AwareDatetime, end: AwareDatetime, limit: int = Query(1000, ge=1, le=5000)):
        return read(request, machine_id, start, end, limit, '''
            SELECT target_at,generated_at,value,unit,model_version,status,reason FROM predictions
            WHERE machine_id=%s AND sensor_id=%s AND target_at >= %s AND target_at < %s
            ORDER BY target_at,generated_at,model_version LIMIT %s
        ''', (machine_id, sensor_id, start, end))
