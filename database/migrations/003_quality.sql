BEGIN;
-- Quality of received readings. Explicit missing readings are counted separately
-- from absent uploads, which cannot be inferred from sequence reservations.
CREATE VIEW sensor_quality_buckets AS
SELECT machine_id, sensor_config_id, sensor_id, widths.seconds AS bucket_seconds,
       date_bin(make_interval(secs => widths.seconds), observed_at,
                '2000-01-01T00:00:00Z'::timestamptz) AS bucket_start,
       count(*) FILTER (WHERE quality = 'ok') AS ok_count,
       count(*) FILTER (WHERE quality = 'missing') AS missing_count,
       count(*) FILTER (WHERE quality = 'error') AS error_count,
       count(*) FILTER (WHERE quality = 'unsupported') AS unsupported_count
FROM measurements
CROSS JOIN (VALUES (10), (60)) AS widths(seconds)
GROUP BY machine_id, sensor_config_id, sensor_id, widths.seconds, bucket_start;
COMMIT;
