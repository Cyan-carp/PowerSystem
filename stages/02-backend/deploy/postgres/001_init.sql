CREATE TABLE IF NOT EXISTS users (
 id BIGSERIAL PRIMARY KEY,
 username VARCHAR(64) NOT NULL UNIQUE,
 password_hash VARCHAR(255) NOT NULL,
 role VARCHAR(32) NOT NULL DEFAULT 'operator',
 real_name VARCHAR(64),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS devices (
 id BIGSERIAL PRIMARY KEY,
 device_code VARCHAR(64) NOT NULL UNIQUE,
 name VARCHAR(128) NOT NULL,
 dev_type VARCHAR(32) NOT NULL,
 vendor VARCHAR(64),
 station_code VARCHAR(64) NOT NULL,
 group_name VARCHAR(64),
 deleted_at TIMESTAMPTZ,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_devices_group ON devices(group_name) WHERE deleted_at IS NULL;
CREATE TABLE IF NOT EXISTS alarm_rules (
 id BIGSERIAL PRIMARY KEY,
 device_id BIGINT NOT NULL REFERENCES devices(id),
 metric VARCHAR(32) NOT NULL,
 operator VARCHAR(2) NOT NULL,
 threshold DOUBLE PRECISION NOT NULL,
 level VARCHAR(16) NOT NULL,
 enabled BOOLEAN NOT NULL DEFAULT true,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 UNIQUE(device_id, metric)
);
CREATE TABLE IF NOT EXISTS alarm_records (
 id BIGSERIAL PRIMARY KEY,
 device_id BIGINT NOT NULL REFERENCES devices(id),
 rule_id BIGINT REFERENCES alarm_rules(id) ON DELETE SET NULL,
 metric VARCHAR(32) NOT NULL,
 level VARCHAR(16) NOT NULL,
 value DOUBLE PRECISION NOT NULL,
 threshold DOUBLE PRECISION NOT NULL,
 status VARCHAR(16) NOT NULL DEFAULT 'unhandled',
 acked_by BIGINT REFERENCES users(id),
 acked_at TIMESTAMPTZ,
 recovered_at TIMESTAMPTZ,
 triggered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_alarm_active ON alarm_records(device_id, metric) WHERE recovered_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_alarm_status_time ON alarm_records(status, triggered_at DESC);
CREATE TABLE IF NOT EXISTS telemetry_inbox (
 id BIGSERIAL PRIMARY KEY,
 run_id VARCHAR(64) NOT NULL,
 device_id BIGINT NOT NULL REFERENCES devices(id),
 seq BIGINT NOT NULL,
 ts_ms BIGINT NOT NULL,
 payload JSONB NOT NULL,
 received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 processed_at TIMESTAMPTZ,
 UNIQUE(run_id, device_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_inbox_pending ON telemetry_inbox(id) WHERE processed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_inbox_pending_device ON telemetry_inbox(device_id,id) WHERE processed_at IS NULL;
