-- Apply once with psql -v ON_ERROR_STOP=1 -f ... . All timestamps are UTC instants.
BEGIN;
CREATE TABLE device_credentials (
 token_sha256 text PRIMARY KEY CHECK (token_sha256 ~ '^[0-9a-f]{64}$'),
 machine_id uuid NOT NULL, expires_at timestamptz NOT NULL, revoked boolean NOT NULL DEFAULT false
);
CREATE TABLE sensor_configs (
 sensor_config_id uuid PRIMARY KEY, machine_id uuid NOT NULL, config jsonb NOT NULL,
 CHECK (jsonb_typeof(config) = 'object')
);
-- Provision through a trusted administrator until S2-03. API role has SELECT only.
CREATE TABLE ingestion_batches (
 machine_id uuid NOT NULL, batch_id uuid NOT NULL, content_sha256 text NOT NULL,
 canonical_body text NOT NULL, response jsonb NOT NULL, received_at timestamptz NOT NULL,
 PRIMARY KEY(machine_id,batch_id)
);
CREATE TABLE ingestion_samples (
 machine_id uuid NOT NULL, boot_session_id uuid NOT NULL, sample_seq bigint NOT NULL,
 batch_id uuid NOT NULL,
 PRIMARY KEY(machine_id,boot_session_id,sample_seq),
 FOREIGN KEY(machine_id,batch_id) REFERENCES ingestion_batches(machine_id,batch_id)
);
CREATE TABLE measurements (
 received_at timestamptz NOT NULL, machine_id uuid NOT NULL, boot_session_id uuid NOT NULL,
 sample_seq bigint NOT NULL, batch_id uuid NOT NULL, sensor_config_id uuid NOT NULL,
 sensor_id text NOT NULL, observed_at timestamptz NOT NULL, monotonic_ms bigint NOT NULL,
 unit text NOT NULL, quality text NOT NULL CHECK (quality IN ('ok','unsupported','missing','error')),
 value numeric, reason text,
 CHECK ((quality='ok' AND value IS NOT NULL AND reason IS NULL) OR (quality<>'ok' AND value IS NULL))
) PARTITION BY RANGE(received_at);
CREATE INDEX measurements_observed_idx ON measurements(machine_id,sensor_id,observed_at);
CREATE INDEX measurements_received_idx ON measurements(received_at);
-- Durable handoff for S3-03; generation prevents lost updates during worker processing.
CREATE TABLE reaggregation_queue (
 machine_id uuid NOT NULL, sensor_id text NOT NULL, bucket_seconds integer NOT NULL CHECK(bucket_seconds IN (10,60)),
 bucket_start timestamptz NOT NULL, generation bigint NOT NULL DEFAULT 1,
 PRIMARY KEY(machine_id,sensor_id,bucket_seconds,bucket_start)
);
CREATE FUNCTION ensure_measurement_partitions(base_day date DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::date)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE d date; partition_name text;
BEGIN
 PERFORM pg_advisory_xact_lock(1030301);
 FOR i IN 0..3 LOOP
  d := base_day+i; partition_name := 'measurements_' || to_char(d,'YYYYMMDD');
  EXECUTE format('CREATE TABLE IF NOT EXISTS %I PARTITION OF measurements FOR VALUES FROM (%L) TO (%L)',
    partition_name, to_char(d,'YYYY-MM-DD') || ' 00:00:00+00', to_char(d+1,'YYYY-MM-DD') || ' 00:00:00+00');
 END LOOP;
END $$;
SELECT ensure_measurement_partitions();
COMMIT;
