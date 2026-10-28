-- Forward-only additive state. Stop all older workers before applying.
ALTER TABLE telemetry_scheduler ADD COLUMN IF NOT EXISTS realtime_last_device_id BIGINT NOT NULL DEFAULT 0;
ALTER TABLE telemetry_scheduler ADD COLUMN IF NOT EXISTS history_enabled BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE telemetry_scheduler ADD COLUMN IF NOT EXISTS history_parallelism INTEGER NOT NULL DEFAULT 8 CHECK(history_parallelism BETWEEN 1 AND 8);
ALTER TABLE telemetry_inbox ADD COLUMN IF NOT EXISTS alarm_evaluation TEXT CHECK(alarm_evaluation IN ('evaluated','historical_suppressed'));
CREATE INDEX IF NOT EXISTS idx_inbox_latest_receipt ON telemetry_inbox(device_id,id DESC);
-- Derived durable source reference, not a queue or a second business ledger.
CREATE TABLE IF NOT EXISTS telemetry_snapshots (
 device_id BIGINT PRIMARY KEY REFERENCES devices(id),
 inbox_id BIGINT NOT NULL REFERENCES telemetry_inbox(id),
 ts_ms BIGINT NOT NULL,
 confirmed_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS telemetry_alarm_progress (
 device_id BIGINT PRIMARY KEY REFERENCES devices(id),
 inbox_id BIGINT NOT NULL REFERENCES telemetry_inbox(id),
 ts_ms BIGINT NOT NULL
);
ALTER TABLE energy_daily ADD COLUMN IF NOT EXISTS covered_ms BIGINT;
ALTER TABLE energy_daily ADD COLUMN IF NOT EXISTS gap_count BIGINT;
ALTER TABLE energy_daily ADD COLUMN IF NOT EXISTS max_gap_ms BIGINT;
