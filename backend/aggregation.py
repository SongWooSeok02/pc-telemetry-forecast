"""Durable full recomputation worker; run with python -m backend.aggregation."""
import logging
import os
import time

import psycopg

KEY = 'machine_id=%s AND sensor_id=%s AND bucket_seconds=%s AND bucket_start=%s'
log = logging.getLogger(__name__)


def process_one(db, *, after_read=None):
    """Process one due bucket in a READ COMMITTED transaction."""
    # Keep only one worker transaction active. Ingestion does not take this lock.
    if not db.execute('SELECT pg_try_advisory_xact_lock(1030302)').fetchone()[0]:
        return False
    job = db.execute('SELECT machine_id,sensor_id,bucket_seconds,bucket_start,generation '
                     'FROM reaggregation_queue WHERE ready_at<=clock_timestamp() '
                     'ORDER BY ready_at LIMIT 1').fetchone()
    if job is None:
        return False
    machine, sensor, seconds, start, generation = job
    key = job[:4]
    try:
        with db.transaction():
            stats = db.execute(
                """SELECT avg(value), max(value), count(value), max(observed_at), max(received_at), max(unit),
                   CASE WHEN count(value)>0 THEN 'ok'
                        WHEN bool_or(quality='error') THEN 'error'
                        WHEN bool_or(quality='missing') THEN 'missing'
                        WHEN bool_or(quality='unsupported') THEN 'unsupported'
                        ELSE 'missing' END
                   FROM measurements """
                'WHERE machine_id=%s AND sensor_id=%s AND observed_at >= %s '
                'AND observed_at < %s + make_interval(secs=>%s)',
                (machine, sensor, start, start, seconds),
            ).fetchone()
            if after_read:
                after_read(db)
            # Lock/compare before publishing. Ingestion that changes the generation
            # before this statement wins; later arrivals recreate the queue entry.
            if not db.execute('DELETE FROM reaggregation_queue WHERE ' + KEY + ' AND generation=%s',
                              (*key, generation)).rowcount:
                return True
            db.execute(
                'INSERT INTO measurement_aggregates '
                '(machine_id,sensor_id,bucket_seconds,bucket_start,average,maximum,valid_count,'
                'observed_at,received_at,unit,quality,updated_at) '
                "SELECT %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,clock_timestamp() "
                "WHERE %s >= transaction_timestamp()-make_interval(hours=>%s) "
                'ON CONFLICT(machine_id,bucket_seconds,sensor_id,bucket_start) DO UPDATE SET '
                'average=excluded.average,maximum=excluded.maximum,valid_count=excluded.valid_count,'
                'updated_at=excluded.updated_at,observed_at=excluded.observed_at,received_at=excluded.received_at,'
                'unit=excluded.unit,quality=excluded.quality,changed_xid=excluded.changed_xid',
                (*key, *stats, start, 720 if seconds == 10 else 2160),
            )
    except psycopg.Error as exc:
        # The savepoint restores the queue item and prevents a partial publication.
        db.execute('UPDATE reaggregation_queue SET attempts=least(attempts+1, 8), '
                   'ready_at=clock_timestamp()+make_interval(secs=>power(2, attempts)) '
                   'WHERE ' + KEY + ' AND generation=%s', (*key, generation))
        log.warning('aggregate_retry sqlstate=%s', exc.sqlstate)
    return True


def main():
    logging.basicConfig(level=logging.INFO)
    while True:
        try:
            with psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=5) as db:
                db.execute("SET LOCAL statement_timeout = '15s'")
                db.execute("SET LOCAL lock_timeout = '5s'")
                worked = process_one(db)
        except psycopg.Error as exc:
            log.error('aggregate_worker_database_error sqlstate=%s', exc.sqlstate)
            time.sleep(5)
        else:
            if not worked:
                time.sleep(1)


if __name__ == '__main__':
    main()
