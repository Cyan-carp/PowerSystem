-- Singleton cursor is scheduling metadata, never a second queue authority.
CREATE TABLE IF NOT EXISTS telemetry_scheduler (
 id INTEGER PRIMARY KEY CHECK (id=1),
 last_device_id BIGINT NOT NULL DEFAULT 0,
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO telemetry_scheduler(id) VALUES(1) ON CONFLICT(id) DO NOTHING;
ALTER TABLE telemetry_inbox ADD COLUMN IF NOT EXISTS missing_since TIMESTAMPTZ;
ALTER TABLE telemetry_inbox ADD COLUMN IF NOT EXISTS missing_checked_at TIMESTAMPTZ;
ALTER TABLE telemetry_inbox ADD COLUMN IF NOT EXISTS missing_attempts BIGINT NOT NULL DEFAULT 0;
ALTER TABLE telemetry_inbox ADD COLUMN IF NOT EXISTS reason_since TIMESTAMPTZ;
ALTER TABLE telemetry_inbox ADD COLUMN IF NOT EXISTS reason_attempts BIGINT NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS telemetry_compensations (
 request_id TEXT PRIMARY KEY,
 inbox_id BIGINT NOT NULL REFERENCES telemetry_inbox(id),
 actor TEXT NOT NULL CHECK(length(trim(actor))>0),
 reason TEXT NOT NULL CHECK(length(trim(reason))>0),
 prior_state JSONB NOT NULL,
 outcome TEXT NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
