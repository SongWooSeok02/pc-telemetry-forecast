"""S4-03 synthetic benchmark. Run from the checkout root with PYTHONPATH=."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import platform
import subprocess
import time

from fastapi.testclient import TestClient
import psycopg
from psycopg.conninfo import make_conninfo

from backend.aggregation import process_one
from backend.app import create_app
from backend.dashboard_history import RAW, ROLLUP
from backend.registration import sha

ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc
PC = '00000000-0000-4000-8000-000000000001'
SENSOR = 'bench-1'


def emit(stage, **fields):
    print(json.dumps({'at': datetime.now(UTC), 'stage': stage, **fields}, default=str), flush=True)


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def load(db, days, seconds):
    require(db.execute("""
        SELECT count(*) = 0 FROM pg_class
        WHERE relnamespace = 'public'::regnamespace
    """).fetchone()[0], 'database must be empty')
    for path in sorted((ROOT / 'database/migrations').glob('*.sql')):
        db.execute(path.read_text(encoding='utf-8-sig'))
    start = db.execute("SELECT date_trunc('day', now()) - %s * interval '1 day'", (days,)).fetchone()[0]
    for day in range(0, days, 4):
        db.execute('SELECT ensure_measurement_partitions(%s)', (start.date() + timedelta(days=day),))
    statement = (ROOT / 'tests/fixtures/s4-03-measurements.sql').read_text(encoding='utf-8-sig')
    total, elapsed = 0, 0
    wal_start = db.execute('SELECT pg_current_wal_insert_lsn()').fetchone()[0]
    for day in range(days):
        offset = day * 86400
        tick = time.perf_counter()
        rows = db.execute(statement, (offset, offset + seconds - 1, start)).rowcount
        duration = time.perf_counter() - tick
        require(rows == 120 * seconds, 'unexpected insert row count')
        total += rows
        elapsed += duration
        emit('load_day', day=day + 1, rows=rows, seconds=duration, rows_per_second=rows / duration)
    wal_bytes = db.execute('SELECT pg_wal_lsn_diff(pg_current_wal_insert_lsn(), %s)', (wal_start,)).fetchone()[0]
    db.execute('ANALYZE measurements')
    sizes = db.execute("""
        SELECT relid::regclass::text, pg_table_size(relid), pg_indexes_size(relid)
        FROM pg_partition_tree('measurements') WHERE isleaf ORDER BY 1
    """).fetchall()
    emit('load', start=start, rows=total, seconds=elapsed, rows_per_second=total / elapsed,
         wal_bytes=int(wal_bytes), heap_toast_bytes=sum(r[1] for r in sizes),
         index_bytes=sum(r[2] for r in sizes), partitions=sizes,
         database_bytes=db.execute('SELECT pg_database_size(current_database())').fetchone()[0])
    return start


def timed(db, stage, query, args=()):
    tick = time.perf_counter()
    result = db.execute(query, args)
    elapsed = time.perf_counter() - tick
    emit(stage, seconds=elapsed, rows=result.rowcount)
    return result


def benchmark(db, dsn, start, days, seconds):
    end = start + timedelta(days=days - 1, seconds=seconds)
    owner = '00000000-0000-4000-8004-000000000000'
    machines = [PC[:-2] + f'{pc:02d}' for pc in range(1, 11)]
    headers = {'Authorization': 'Bearer synthetic-s403-owner'}
    db.execute("INSERT INTO user_sessions VALUES (%s,%s,clock_timestamp()+interval '1 day',false)",
               (sha('synthetic-s403-owner'), owner))
    for machine in machines:
        db.execute('INSERT INTO machines VALUES (%s,%s,false)', (machine, owner))

    db.execute("""
        INSERT INTO sensor_configs (sensor_config_id,machine_id,config)
        SELECT ('00000000-0000-4000-8003-' || lpad(pc::text, 12, '0'))::uuid,
               ('00000000-0000-4000-8000-' || lpad(pc::text, 12, '0'))::uuid,
               '{"sensors":{"bench-1":{"unit":"percent"}}}'::jsonb
        FROM generate_series(1, 10) pc
    """)

    # Seed known-correct aligned buckets from the fixture formula. Worker cost
    # is measured separately below with a real queued recomputation.
    result = timed(db, 'seed_rollups', """
        INSERT INTO measurement_aggregates
          (machine_id,sensor_id,bucket_seconds,bucket_start,average,maximum,valid_count,
           updated_at,observed_at,received_at,unit,quality)
        SELECT ('00000000-0000-4000-8000-' || lpad(pc::text, 12, '0'))::uuid,
               'bench-' || sensor, width, bucket,
               pc + sensor + sec_offset %% 60 + (width - 1) / 2.0,
               pc + sensor + sec_offset %% 60 + width - 1, width,
               clock_timestamp(), bucket + (width-1) * interval '1 second',
               bucket + (width-1) * interval '1 second', 'percent', 'ok'
        FROM generate_series(0, %s - 1) day
        CROSS JOIN unnest(ARRAY[10,60]) width
        CROSS JOIN LATERAL generate_series(0, %s - 1, width) sec_offset
        CROSS JOIN generate_series(1,10) pc
        CROSS JOIN generate_series(1,12) sensor
        CROSS JOIN LATERAL
          (SELECT %s::timestamptz + (day * 86400 + sec_offset) * interval '1 second' AS bucket) t
    """, (days, seconds, start))
    require(result.rowcount == days * 120 * (seconds // 10 + seconds // 60), 'rollup count')
    db.execute('ANALYZE measurement_aggregates')
    emit('rollup_size', bytes=db.execute(
        "SELECT pg_total_relation_size('measurement_aggregates')").fetchone()[0])

    for label, query, params, expected in [
        ('raw_1m', RAW, (PC, SENSOR, end-timedelta(minutes=1), end, start, None, None, None), 60),
        ('raw_6h', RAW, (PC, SENSOR, end-timedelta(hours=6), end, start, None, None, None),
         min(seconds, 21600)),
        ('rollup_1m', ROLLUP, (PC, SENSOR, 60, start, end) * 2 + (60, None, None, None, None, 60),
         days * seconds // 60),
    ]:
        plan = db.execute('EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) ' + query, params).fetchone()[0][0]
        emit('plan', query=label, expected_rows=expected, plan=plan)
        require(plan['Plan']['Actual Rows'] == expected, f'unexpected {label} plan row count')

    with TestClient(create_app(dsn, clock=lambda: end)) as client:
        def poll(machine, cursor):
            path = f'/v1/dashboard/machines/{machine}/sensors/{SENSOR}/history'
            params = {'cursor': cursor} if cursor else {
                'resolution': 'raw', 'from': (end-timedelta(minutes=1)).isoformat(), 'to': end.isoformat(),
            }
            tick = time.perf_counter()
            response = client.get(path, headers=headers, params=params)
            elapsed = time.perf_counter() - tick
            require(response.status_code == 200, f'poll HTTP {response.status_code}')
            payload = response.json()
            require(len(payload['points']) == (0 if cursor else 60), 'unexpected poll response size')
            return payload['nextCursor'], elapsed

        cursors = [None] * len(machines)
        started = time.perf_counter()
        cycles = 2 if days == 2 else 10
        with ThreadPoolExecutor(max_workers=len(machines)) as pool:
            for cycle in range(cycles):
                time.sleep(max(0, started + cycle * 3 - time.perf_counter()))
                tick = time.perf_counter()
                results = list(pool.map(poll, machines, cursors))
                duration = time.perf_counter() - tick
                cursors, latencies = zip(*results)
                emit('poll_cycle', cycle=cycle, clients=len(machines), start_offset_seconds=tick-started,
                     seconds=duration, request_seconds=latencies, within_cadence=duration < 3)
                require(duration < 3, '3-second poll cycle missed')

    db.execute("SET statement_timeout = '30s'")
    db.execute("SET lock_timeout = '5s'")
    oldest = 'measurements_' + start.strftime('%Y%m%d')
    as_of = start + timedelta(days=8)
    lookup = db.execute("""EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
        SELECT relid FROM pg_partition_tree('measurements') WHERE isleaf AND relid=to_regclass(%s)
    """, (oldest,)).fetchone()[0][0]
    emit('partition_lookup', plan=lookup)
    db.execute("""INSERT INTO reaggregation_queue(machine_id,sensor_id,bucket_seconds,bucket_start)
                  VALUES (%s,%s,60,%s)""", (PC, SENSOR, start))
    timed(db, 'prune_pending', 'SELECT prune_telemetry(%s)', (as_of,))
    require(db.execute('SELECT to_regclass(%s)', (oldest,)).fetchone()[0] is not None, 'pending raw lost')
    with db.transaction():
        queue_age = db.execute(
            'SELECT extract(epoch FROM clock_timestamp()-ready_at) FROM reaggregation_queue'
        ).fetchone()[0]
        tick = time.perf_counter()
        require(process_one(db), 'worker did not take job')
    emit('reaggregate', seconds=time.perf_counter()-tick, queue_age_seconds=float(queue_age))
    aggregate = db.execute("""SELECT average,maximum,valid_count FROM measurement_aggregates
        WHERE machine_id=%s AND sensor_id=%s AND bucket_seconds=60 AND bucket_start=%s
    """, (PC, SENSOR, start)).fetchone()
    require(aggregate == (31.5, 61, 60), 'incorrect recomputed aggregate')
    require(db.execute('SELECT count(*) FROM reaggregation_queue').fetchone()[0] == 0, 'pending jobs remain')

    with psycopg.connect(dsn, connect_timeout=5) as reader:
        reader.execute('SELECT 1 FROM public.measurements LIMIT 1').fetchone()
        tick = time.perf_counter()
        try:
            db.execute('SELECT prune_telemetry(%s)', (as_of,))
        except psycopg.errors.LockNotAvailable:
            emit('prune_reader_lock', seconds=time.perf_counter()-tick, sqlstate='55P03')
        else:
            raise RuntimeError('expected lock timeout while reader holds parent lock')
    before = db.execute('SELECT pg_database_size(current_database())').fetchone()[0]
    timed(db, 'prune_after_reader', 'SELECT prune_telemetry(%s)', (as_of,))
    require(db.execute('SELECT to_regclass(%s)', (oldest,)).fetchone()[0] is None, 'expiry did not drop')
    following = 'measurements_' + (start + timedelta(days=1)).strftime('%Y%m%d')
    require(db.execute('SELECT to_regclass(%s)', (following,)).fetchone()[0] is not None, 'retained day lost')
    after = db.execute('SELECT pg_database_size(current_database())').fetchone()[0]
    emit('partition_removed', partition=oldest, database_bytes_before=before, database_bytes_after=after,
         database_bytes_reclaimed=before-after)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--smoke', action='store_true')
    args = parser.parse_args()
    dsn = make_conninfo(os.environ['S403_DSN'], options='-c timezone=UTC -c search_path=public')
    days, seconds = (2, 60) if args.smoke else (7, 86400)
    db = psycopg.connect(dsn, autocommit=True, connect_timeout=5)
    with db:
        name = db.execute('SELECT current_database()').fetchone()[0]
        require(name.startswith('s403_bench_'), 'dedicated s403_bench_* database required')
        require(db.execute("SELECT pg_try_advisory_lock(40303)").fetchone()[0], 'benchmark already running')
        emit('environment', git=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
             platform=platform.platform(), processor=os.environ.get('PROCESSOR_IDENTIFIER', platform.processor()),
             logical_cpus=os.cpu_count(), python=platform.python_version(), psycopg=psycopg.__version__,
             database=db.execute('SELECT version()').fetchone()[0],
             settings=dict(db.execute("""
                 SELECT name, current_setting(name) FROM pg_settings
                 WHERE name IN ('shared_buffers','work_mem','max_wal_size','synchronous_commit',
                                'fsync','max_parallel_workers_per_gather')
             """).fetchall()), smoke=args.smoke, expected_rows=120 * days * seconds)
        db.execute("SET statement_timeout = '1h'")
        start = load(db, days, seconds)
        benchmark(db, dsn, start, days, seconds)
        emit('result', result='pass')


if __name__ == '__main__':
    try:
        main()
    except (psycopg.Error, RuntimeError) as exc:
        emit('result', result='fail', error=type(exc).__name__, detail=str(exc) if isinstance(exc, RuntimeError) else None, sqlstate=getattr(exc, 'sqlstate', None))
        raise SystemExit(1) from None
