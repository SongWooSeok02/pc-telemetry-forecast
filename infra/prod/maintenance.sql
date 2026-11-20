\set ON_ERROR_STOP on
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';
SELECT ensure_measurement_partitions();
COMMIT;
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';
SELECT prune_telemetry_metadata();
SELECT prune_telemetry();
COMMIT;
