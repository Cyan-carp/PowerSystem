-- Repeatable maintenance entry point. Mark every processed sample's date and
-- adjacent dates within the maximum integration gap for bounded API rebuild.
INSERT INTO energy_dirty_days(device_id, day, changed_at)
SELECT dates.device_id, dates.day, clock_timestamp()
FROM (
  SELECT DISTINCT i.device_id,
    (to_timestamp((i.ts_ms + offsets.delta_ms)/1000.0) AT TIME ZONE 'Asia/Shanghai')::date AS day
  FROM telemetry_inbox AS i
  CROSS JOIN (VALUES (-15000), (0), (15000)) AS offsets(delta_ms)
  WHERE i.processed_at IS NOT NULL
) AS dates
ON CONFLICT(device_id, day) DO UPDATE SET changed_at=clock_timestamp();
