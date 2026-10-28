CREATE TABLE IF NOT EXISTS energy_daily (
 device_id BIGINT NOT NULL REFERENCES devices(id),
 day DATE NOT NULL,
 kwh DOUBLE PRECISION NOT NULL DEFAULT 0,
 segments_count INTEGER NOT NULL DEFAULT 0,
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(device_id, day)
);
CREATE TABLE IF NOT EXISTS energy_dirty_days (
 device_id BIGINT NOT NULL REFERENCES devices(id),
 day DATE NOT NULL,
 changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(device_id, day)
);
CREATE INDEX IF NOT EXISTS idx_energy_dirty_changed ON energy_dirty_days(changed_at);
CREATE INDEX IF NOT EXISTS idx_inbox_energy ON telemetry_inbox(device_id,ts_ms,id) WHERE processed_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_inbox_energy_start ON telemetry_inbox(ts_ms) WHERE processed_at IS NOT NULL;
-- Historical aggregates are retained. Explicit backfill is a separate audited operation.
