"""Read the latest accepted batch for an authorized dashboard machine."""
from uuid import UUID

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from .registration import owned, session
from .user_session import NO_STORE


def install_dashboard_current(app, connect, rejected):
    @app.get('/v1/dashboard/machines/{machine_id}/current')
    def current(machine_id: UUID, request: Request):
        with connect() as db:
            user = session(rejected, db, request)
            owned(rejected, db, user, machine_id)
            latest = db.execute(
                'SELECT batch_id, received_at FROM ingestion_batches WHERE machine_id=%s '
                'ORDER BY received_at DESC, batch_id DESC LIMIT 1', (machine_id,),
            ).fetchone()
            rows = [] if latest is None else db.execute("""
                SELECT DISTINCT ON (r.sensor_id) r.sensor_id, r.value, r.unit, r.quality, r.observed_at
                FROM measurements r WHERE r.machine_id=%s AND r.batch_id=%s AND r.received_at=%s
                ORDER BY r.sensor_id, r.observed_at DESC, r.sample_seq DESC
            """, (machine_id, *latest)).fetchall()
        readings = [dict(sensorId=sensor, label=sensor, value=float(value) if value is not None else None,
                         unit=unit, quality=quality, observedAt=observed)
                    for sensor, value, unit, quality, observed in rows]
        return JSONResponse(jsonable_encoder(dict(machineId=machine_id, machineName=str(machine_id),
                            receivedAt=latest[1] if latest else None, readings=readings)), headers=NO_STORE)
