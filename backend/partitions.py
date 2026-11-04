"""Run hourly as a separate migration-owner process; nonzero exit must alert."""
import logging
import os
import psycopg


def main():
    logging.basicConfig(level=logging.INFO)
    try:
        with psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=5) as db:
            db.execute("SET LOCAL lock_timeout = '5s'")
            db.execute("SET LOCAL statement_timeout = '30s'")
            db.execute('SELECT ensure_measurement_partitions()')
        logging.info('partition_maintenance_success horizon_days=3')
    except Exception as exc:
        # Connection errors can include hosts/users/DSNs; keep diagnostics bounded.
        logging.error('partition_maintenance_failed type=%s sqlstate=%s',
                      type(exc).__name__, getattr(exc, 'sqlstate', None))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
