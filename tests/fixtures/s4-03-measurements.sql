-- 10 machines x 12 sensors per second, using the production table and indexes.
INSERT INTO measurements
    (received_at, machine_id, boot_session_id, sample_seq, batch_id, sensor_config_id,
     sensor_id, observed_at, monotonic_ms, unit, quality, value)
SELECT at, ('00000000-0000-4000-8000-' || lpad(pc::text, 12, '0'))::uuid,
       '00000000-0000-4000-8001-000000000000', seq,
       '00000000-0000-4000-8002-000000000000',
       '00000000-0000-4000-8003-000000000000',
       'bench-' || sensor, at, seq * 1000, 'percent', 'ok', pc + sensor + seq %% 60
FROM generate_series(%s::bigint, %s::bigint) seq
CROSS JOIN generate_series(1, 10) pc
CROSS JOIN generate_series(1, 12) sensor
CROSS JOIN LATERAL (SELECT %s::timestamptz + seq * interval '1 second' AS at) t
