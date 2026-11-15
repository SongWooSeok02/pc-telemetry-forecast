"""Periodically write forecasts; HTTP handlers only read committed results."""

import hashlib
import json
import logging
import math
import os
import time
from datetime import timedelta
from pathlib import Path

import psycopg

LOG = logging.getLogger(__name__)
CADENCE = 10
HORIZON = timedelta(seconds=60)
FRESHNESS = timedelta(seconds=10)
BASELINE = 'builtin:persistence:v1'
NO_MODEL = 'builtin:none'


def slot_at(now):
    return now.replace(second=now.second // CADENCE * CADENCE, microsecond=0)


def value_of(row, unit):
    if row[2] != 'ok' or row[3] != unit or row[1] is None:
        return None
    value = float(row[1])
    return value if math.isfinite(value) else None


def predict(rows, unit, now, model, artifact_root):
    if not rows or now - rows[0][0] > FRESHNESS:
        return None, NO_MODEL, 'unavailable', 'stale_or_missing_input'
    latest = value_of(rows[0], unit)
    if latest is None:
        return None, NO_MODEL, 'unavailable', 'invalid_latest_input'
    if model is None:
        return latest, BASELINE, 'fallback', 'no_active_model'

    version, path, digest, spec = model
    try:
        count = spec['window_seconds']
        if (set(spec) != {'window_seconds', 'step_seconds', 'horizon_seconds', 'unit'}
                or type(count) is not int or not 1 <= count <= 300
                or spec['step_seconds'] != 1 or spec['horizon_seconds'] != 60
                or spec['unit'] != unit):
            raise ValueError('incompatible input specification')
        root = artifact_root.resolve()
        artifact = (root / path).resolve()
        if not artifact.is_relative_to(root) or not artifact.is_file():
            raise ValueError('artifact must be a file in the model directory')
        with artifact.open('rb') as stream:
            data = stream.read(65537)
        if len(data) > 65536 or hashlib.sha256(data).hexdigest() != digest:
            raise ValueError('invalid artifact size or checksum')
        params = json.loads(data)
        kind = params['kind']
        if kind == 'persistence' and count == 1:
            return latest, version, 'ok', None
        if kind not in ('moving_average', 'linear') or (kind == 'moving_average' and count != 300):
            raise ValueError('unsupported artifact')
        if len(rows) < count:
            return latest, BASELINE, 'fallback', 'incomplete_window'
        values = []
        origin = rows[0][0].replace(microsecond=0)
        for i, row in enumerate(rows[:count]):
            value = value_of(row, unit)
            if (value is None or row[4:] != rows[0][4:]
                    or row[0].replace(microsecond=0) != origin - timedelta(seconds=i)):
                return latest, BASELINE, 'fallback', 'invalid_window'
            values.append(value)
        # Reject ambiguous input even if the duplicate falls just outside the slice.
        if len(rows) > count and rows[count][0].replace(microsecond=0) == origin - timedelta(seconds=count-1):
            return latest, BASELINE, 'fallback', 'invalid_window'
        if kind == 'moving_average':
            result = math.fsum(value / count for value in values)
        else:
            weights = params['weights']
            if len(weights) != count:
                raise ValueError('wrong number of weights')
            result = math.fsum(float(w) * x for w, x in zip(weights, reversed(values))) + float(params['bias'])
        if not math.isfinite(result):
            raise ValueError('non-finite output')
        return result, version, 'ok', None
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, ArithmeticError):
        LOG.warning('Forecast artifact or input specification rejected (version=%r)', version)
        return latest, BASELINE, 'fallback', 'model_rejected'


def run_cycle(db, artifact_root, *, clock=None):
    """Commit a whole slot or roll it back. Never replay missed slots."""
    if clock is None:
        def clock():
            return db.execute('SELECT clock_timestamp()').fetchone()[0]
    with db.transaction():
        db.execute("SET LOCAL statement_timeout = '5s'")
        db.execute("SET LOCAL lock_timeout = '2s'")
        db.execute("SET LOCAL TIME ZONE 'UTC'")
        if not db.execute('SELECT pg_try_advisory_xact_lock(1030501)').fetchone()[0]:
            return False
        now = clock()
        slot = slot_at(now)
        if not db.execute("""
            INSERT INTO forecast_cycle (last_slot) VALUES (%s)
            ON CONFLICT (singleton) DO UPDATE SET last_slot=excluded.last_slot
            WHERE forecast_cycle.last_slot < excluded.last_slot RETURNING last_slot
        """, (slot,)).fetchone():
            return False
        targets = db.execute("""
            SELECT t.machine_id,t.sensor_id,t.unit,m.version,m.artifact_path,m.artifact_sha256,m.input_spec
            FROM forecast_targets t JOIN machines d ON d.machine_id=t.machine_id AND NOT d.deleted
            LEFT JOIN forecast_models m ON m.machine_id=t.machine_id AND m.sensor_id=t.sensor_id AND m.active
            ORDER BY t.machine_id,t.sensor_id
        """).fetchall()
        for machine, sensor, unit, *model in targets:
            cutoff = clock()
            rows = db.execute("""
                SELECT observed_at,value,quality,unit,boot_session_id,sensor_config_id FROM measurements
                WHERE machine_id=%s AND sensor_id=%s AND observed_at BETWEEN %s AND %s
                  AND received_at BETWEEN %s AND %s
                ORDER BY observed_at DESC,received_at DESC,boot_session_id,sample_seq DESC LIMIT 301
            """, (machine, sensor, cutoff - timedelta(seconds=310), cutoff,
                  cutoff - timedelta(seconds=310), cutoff)).fetchall()
            value, version, status, reason = predict(rows, unit, cutoff, model if model[0] else None, artifact_root)
            generated = clock()
            db.execute("""
                INSERT INTO predictions
                    (machine_id,sensor_id,slot_at,target_at,generated_at,value,model_version,status,reason)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (machine, sensor, slot, generated + HORIZON, generated, value, version, status, reason))
    return True


def main():
    logging.basicConfig(level=logging.INFO)
    root = Path(os.environ.get('FORECAST_MODEL_DIR', '/models'))
    while True:
        try:
            with psycopg.connect(os.environ.get('DATABASE_URL', ''), connect_timeout=5) as db:
                run_cycle(db, root)
        except psycopg.Error:
            LOG.exception('Forecast cycle failed; will retry on next tick')
        time.sleep(CADENCE - time.time() % CADENCE)


if __name__ == '__main__':
    main()
