\set ON_ERROR_STOP on
SET statement_timeout = '5s';
SELECT COALESCE(extract(epoch FROM clock_timestamp() - min(ready_at)), 0) <= 120 AS aggregation_ok
  FROM reaggregation_queue \gset
SELECT COALESCE(max(last_slot) >= clock_timestamp() - interval '2 minutes', false) AS forecast_ok
  FROM forecast_cycle \gset
SELECT :'aggregation_ok'::boolean AND :'forecast_ok'::boolean AS workers_ok \gset
\if :aggregation_ok
\else
  \warn aggregation_queue_overdue
\endif
\if :forecast_ok
\else
  \warn forecast_cycle_stale
\endif
\if :workers_ok
\else
  DO $$ BEGIN RAISE EXCEPTION 'worker health check failed'; END $$;
\endif
