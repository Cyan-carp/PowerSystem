CREATE TABLE IF NOT EXISTS prediction_records (
 id BIGSERIAL PRIMARY KEY,
 device_id BIGINT NOT NULL REFERENCES devices(id),
 window_end_ms BIGINT NOT NULL,
 probability DOUBLE PRECISION NOT NULL CHECK (probability >= 0 AND probability <= 1),
 threshold DOUBLE PRECISION NOT NULL CHECK (threshold >= 0 AND threshold <= 1),
 risk_level VARCHAR(8) NOT NULL CHECK (risk_level IN ('low','high')),
 model_version VARCHAR(64) NOT NULL,
 top_factors JSONB NOT NULL,
 source VARCHAR(32) NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 UNIQUE(device_id,window_end_ms,model_version)
);
CREATE INDEX IF NOT EXISTS idx_prediction_device_time ON prediction_records(device_id,window_end_ms DESC);
