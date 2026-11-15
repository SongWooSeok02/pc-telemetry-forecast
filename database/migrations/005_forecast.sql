BEGIN;
CREATE TABLE forecast_models (
 machine_id uuid NOT NULL REFERENCES machines(machine_id),
 sensor_id text NOT NULL, version text NOT NULL CHECK (version <> '' AND version NOT LIKE 'builtin:%'),
 training_run_id text NOT NULL CHECK (training_run_id <> ''),
 trained_from timestamptz NOT NULL, trained_until timestamptz NOT NULL,
 registered_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 artifact_path text NOT NULL CHECK (artifact_path <> ''),
 artifact_sha256 text NOT NULL CHECK (artifact_sha256 ~ '^[0-9a-f]{64}$'),
 input_spec jsonb NOT NULL CHECK (jsonb_typeof(input_spec) = 'object'),
 evaluation jsonb NOT NULL CHECK (jsonb_typeof(evaluation) = 'object'),
 active boolean NOT NULL DEFAULT false,
 CHECK (trained_from < trained_until),
 PRIMARY KEY (machine_id, sensor_id, version)
);
CREATE UNIQUE INDEX forecast_one_active ON forecast_models(machine_id, sensor_id) WHERE active;
CREATE FUNCTION preserve_forecast_model_version() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP = 'DELETE' THEN
  RAISE EXCEPTION 'Forecast model versions cannot be deleted';
 END IF;
 IF (to_jsonb(NEW) - 'active') IS DISTINCT FROM (to_jsonb(OLD) - 'active') THEN
  RAISE EXCEPTION 'Register a new version to change model metadata';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER forecast_model_version BEFORE UPDATE OR DELETE ON forecast_models
 FOR EACH ROW EXECUTE FUNCTION preserve_forecast_model_version();
CREATE TABLE forecast_targets (
 machine_id uuid NOT NULL REFERENCES machines(machine_id),
 sensor_id text NOT NULL, unit text NOT NULL,
 PRIMARY KEY (machine_id, sensor_id)
);
-- Preserve existing S3-03 results; only worker rows carry a cadence slot.
ALTER TABLE predictions ADD COLUMN slot_at timestamptz,
 ADD COLUMN status text NOT NULL DEFAULT 'ok',
 ADD COLUMN reason text,
 ALTER COLUMN value DROP NOT NULL,
 ADD CHECK ((status = 'ok' AND value IS NOT NULL AND reason IS NULL)
         OR (status = 'fallback' AND value IS NOT NULL AND reason IS NOT NULL)
         OR (status = 'unavailable' AND value IS NULL AND reason IS NOT NULL)),
 ADD CHECK (slot_at IS NULL OR target_at = generated_at + interval '60 seconds');
CREATE UNIQUE INDEX forecast_one_per_slot ON predictions(machine_id, sensor_id, slot_at);
CREATE TABLE forecast_cycle (
 singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
 last_slot timestamptz NOT NULL
);
-- Use the worker lock so an activation cannot overtake a cycle that has read
-- the previous active version. A failed switch rolls back both updates.
CREATE FUNCTION activate_forecast_model(target_machine uuid, target_sensor text, target_version text)
RETURNS void LANGUAGE plpgsql AS $$
BEGIN
 PERFORM pg_advisory_xact_lock(1030501);
 UPDATE forecast_models SET active=false
  WHERE machine_id=target_machine AND sensor_id=target_sensor AND active;
 UPDATE forecast_models SET active=true
  WHERE machine_id=target_machine AND sensor_id=target_sensor AND version=target_version;
 IF NOT FOUND THEN
  RAISE EXCEPTION 'Unknown forecast model for target';
 END IF;
END $$;
COMMIT;
