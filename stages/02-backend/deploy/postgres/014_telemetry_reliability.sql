-- NULL is deliberately preserved for historical rows, without guessing success.
ALTER TABLE telemetry_inbox ADD COLUMN IF NOT EXISTS business_state TEXT;
ALTER TABLE telemetry_inbox ALTER COLUMN business_state SET DEFAULT 'pending';
ALTER TABLE telemetry_inbox ADD COLUMN IF NOT EXISTS td_confirmed_at TIMESTAMPTZ;
ALTER TABLE telemetry_inbox ADD COLUMN IF NOT EXISTS first_failed_at TIMESTAMPTZ;
ALTER TABLE telemetry_inbox ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ;
ALTER TABLE telemetry_inbox ADD COLUMN IF NOT EXISTS attempts BIGINT NOT NULL DEFAULT 0;
ALTER TABLE telemetry_inbox ADD COLUMN IF NOT EXISTS failure_code TEXT;
-- Repeated by the application migrator, with no procedural SQL or history rewrite.
ALTER TABLE telemetry_inbox DROP CONSTRAINT IF EXISTS inbox_business_state_check;
ALTER TABLE telemetry_inbox ADD CONSTRAINT inbox_business_state_check CHECK (
 business_state IS NULL OR
 (business_state IN ('pending','retry','success','quarantined') AND
 ((business_state IN ('success','quarantined')) = (processed_at IS NOT NULL)))
) NOT VALID;
CREATE INDEX IF NOT EXISTS idx_inbox_received ON telemetry_inbox(received_at DESC);
CREATE INDEX IF NOT EXISTS idx_inbox_success ON telemetry_inbox(processed_at DESC) WHERE business_state='success';
CREATE INDEX IF NOT EXISTS idx_inbox_restore ON telemetry_inbox(device_id,id DESC) WHERE business_state='success' OR (business_state IS NULL AND processed_at IS NOT NULL);
CREATE INDEX IF NOT EXISTS idx_inbox_energy_sources ON telemetry_inbox(device_id,ts_ms,id);
ALTER TABLE energy_daily ADD COLUMN IF NOT EXISTS source_quality TEXT NOT NULL DEFAULT 'legacy_unknown';
CREATE TABLE IF NOT EXISTS telemetry_silence_windows (
 id BIGSERIAL PRIMARY KEY,
 device_id BIGINT NOT NULL REFERENCES devices(id),
 starts_at TIMESTAMPTZ NOT NULL,
 ends_at TIMESTAMPTZ NOT NULL,
 reason TEXT NOT NULL CHECK (length(trim(reason))>0),
 created_by TEXT NOT NULL CHECK (length(trim(created_by))>0),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 CHECK (ends_at>starts_at)
);
CREATE INDEX IF NOT EXISTS idx_telemetry_silence_device ON telemetry_silence_windows(device_id,ends_at);
