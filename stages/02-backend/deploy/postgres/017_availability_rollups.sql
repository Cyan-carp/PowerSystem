-- Additive derived statistics only. Raw telemetry remains authoritative.
CREATE TABLE IF NOT EXISTS availability_rollup_state (
 id INTEGER PRIMARY KEY CHECK(id=1),
 worker_enabled BOOLEAN NOT NULL DEFAULT TRUE,
 read_enabled BOOLEAN NOT NULL DEFAULT FALSE,
 seeded_hour_ms BIGINT NOT NULL DEFAULT 0,
 swept_at TIMESTAMPTZ
);
INSERT INTO availability_rollup_state(id) VALUES(1) ON CONFLICT(id) DO NOTHING;
CREATE TABLE IF NOT EXISTS availability_hourly (
 device_id BIGINT NOT NULL REFERENCES devices(id),
 hour_ms BIGINT NOT NULL CHECK(hour_ms % 3600000=0),
 rule_version TEXT NOT NULL DEFAULT 'availability-v1',
 generation BIGINT NOT NULL DEFAULT 1,
 computed_generation BIGINT NOT NULL DEFAULT 0,
 covered_ms BIGINT,
 running_ms BIGINT,
 stopped_ms BIGINT,
 fault_ms BIGINT,
 unknown_ms BIGINT,
 computed_at TIMESTAMPTZ,
 PRIMARY KEY(device_id,hour_ms,rule_version),
 CHECK(covered_ms IS NULL OR (covered_ms>=0 AND covered_ms<=3600000 AND running_ms+stopped_ms+fault_ms=covered_ms AND unknown_ms=3600000-covered_ms))
);
CREATE INDEX IF NOT EXISTS idx_availability_hourly_pending ON availability_hourly(hour_ms DESC,device_id) WHERE generation<>computed_generation;
