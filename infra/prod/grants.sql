BEGIN;
GRANT USAGE ON SCHEMA public TO telemetry_app;
GRANT SELECT ON machines, user_sessions, registration_codes, device_credentials,
    credential_audit, sensor_configs, ingestion_batches, ingestion_samples, measurements,
    reaggregation_queue, measurement_aggregates, predictions, forecast_models,
    forecast_targets, forecast_cycle TO telemetry_app;
GRANT INSERT ON machines, registration_codes, device_credentials, credential_audit,
    ingestion_batches, ingestion_samples, measurements, reaggregation_queue,
    measurement_aggregates, predictions, forecast_cycle TO telemetry_app;
GRANT UPDATE ON reaggregation_queue, measurement_aggregates, forecast_cycle TO telemetry_app;
GRANT UPDATE (deleted) ON machines TO telemetry_app;
GRANT UPDATE (revoked) ON device_credentials TO telemetry_app;
GRANT UPDATE (consumed) ON registration_codes TO telemetry_app;
GRANT DELETE ON reaggregation_queue TO telemetry_app;
GRANT USAGE ON SEQUENCE credential_audit_event_id_seq TO telemetry_app;
COMMIT;
