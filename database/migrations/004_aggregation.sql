BEGIN;
ALTER TABLE reaggregation_queue
 ADD COLUMN attempts integer NOT NULL DEFAULT 0,
 ADD COLUMN ready_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 ADD COLUMN changed_xid xid8;
ALTER TABLE measurements ADD COLUMN changed_xid xid8;
ALTER TABLE measurements ALTER COLUMN changed_xid SET DEFAULT pg_current_xact_id();
CREATE FUNCTION mark_reaggregation_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 NEW.changed_xid := pg_current_xact_id();
 RETURN NEW;
END $$;
CREATE TRIGGER reaggregation_change BEFORE INSERT OR UPDATE OF generation ON reaggregation_queue
 FOR EACH ROW EXECUTE FUNCTION mark_reaggregation_change();
CREATE INDEX reaggregation_ready_idx ON reaggregation_queue(ready_at);
CREATE INDEX reaggregation_read_idx ON reaggregation_queue(machine_id,bucket_seconds,sensor_id,bucket_start);
CREATE TABLE measurement_aggregates (
 machine_id uuid NOT NULL, sensor_id text NOT NULL, bucket_seconds integer NOT NULL CHECK(bucket_seconds IN (10,60)),
 bucket_start timestamptz NOT NULL, average numeric, maximum numeric, valid_count bigint NOT NULL,
 updated_at timestamptz NOT NULL, observed_at timestamptz, received_at timestamptz, unit text,
 quality text NOT NULL, changed_xid xid8 NOT NULL DEFAULT pg_current_xact_id(),
 PRIMARY KEY(machine_id,bucket_seconds,sensor_id,bucket_start)
);
CREATE INDEX measurement_aggregate_retention_idx ON measurement_aggregates(bucket_seconds,bucket_start);
CREATE TABLE predictions (
 machine_id uuid NOT NULL REFERENCES machines(machine_id),
 sensor_id text NOT NULL, target_at timestamptz NOT NULL, generated_at timestamptz NOT NULL,
 value numeric NOT NULL, model_version text NOT NULL,
 PRIMARY KEY(machine_id,sensor_id,target_at,generated_at,model_version)
);
CREATE INDEX ingestion_batches_received_idx ON ingestion_batches(received_at) WHERE canonical_body <> '';

CREATE FUNCTION prune_telemetry_metadata(as_of timestamptz DEFAULT clock_timestamp())
RETURNS void LANGUAGE sql AS $$
 DELETE FROM measurement_aggregates WHERE bucket_seconds=10 AND bucket_start<as_of-interval '720 hours';
 DELETE FROM measurement_aggregates WHERE bucket_seconds=60 AND bucket_start<as_of-interval '2160 hours';
 UPDATE ingestion_batches SET canonical_body=''
  WHERE canonical_body<>'' AND received_at<as_of-interval '168 hours';
$$;

-- Drop only standard, attached UTC partitions with no pending work that needs their rows.
CREATE FUNCTION prune_telemetry(as_of timestamptz DEFAULT clock_timestamp())
RETURNS void LANGUAGE plpgsql AS $$
DECLARE p record; day_start timestamp; blocked boolean;
BEGIN
 -- Serialize partition DDL and wait for in-flight measurement inserts and worker reads.
 PERFORM pg_advisory_xact_lock(1030301);
 LOCK TABLE ONLY measurements IN ACCESS EXCLUSIVE MODE;
 FOR p IN
  SELECT n.nspname, c.relname, pg_get_expr(c.relpartbound,c.oid) AS bounds
  FROM pg_inherits i JOIN pg_class c ON c.oid=i.inhrelid JOIN pg_namespace n ON n.oid=c.relnamespace
  WHERE i.inhparent='measurements'::regclass AND c.relname ~ '^measurements_[0-9]{8}$'
 LOOP
  day_start := to_date(right(p.relname,8),'YYYYMMDD')::timestamp;
  CONTINUE WHEN (day_start + interval '1 day') AT TIME ZONE 'UTC' > as_of - interval '168 hours';
  -- Verify the generated name actually matches the range.
  CONTINUE WHEN p.bounds <> format('FOR VALUES FROM (%L) TO (%L)',
    day_start AT TIME ZONE 'UTC', (day_start + interval '1 day') AT TIME ZONE 'UTC');
  EXECUTE format('SELECT EXISTS (
   SELECT 1 FROM reaggregation_queue q JOIN %I.%I m
     ON m.machine_id=q.machine_id AND m.sensor_id=q.sensor_id
     AND m.observed_at>=q.bucket_start
     AND m.observed_at<q.bucket_start+make_interval(secs=>q.bucket_seconds))', p.nspname,p.relname)
   INTO blocked;
  IF NOT blocked THEN
   EXECUTE format('DROP TABLE %I.%I',p.nspname,p.relname);
  END IF;
 END LOOP;
END $$ SET TimeZone TO 'UTC' SET DateStyle TO 'ISO, YMD';
COMMIT;
