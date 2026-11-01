-- 预测质量：真值回填、日指标、模型注册与漂移事件。
-- 全部为派生统计，prediction_records 仍是预测事实源，本文件不修改任何既有表。
CREATE TABLE IF NOT EXISTS prediction_outcomes (
 prediction_id BIGINT PRIMARY KEY,
 device_id BIGINT NOT NULL,
 device_code TEXT NOT NULL,
 station_code TEXT NOT NULL,
 window_end_ms BIGINT NOT NULL,
 model_version TEXT NOT NULL,
 probability DOUBLE PRECISION NOT NULL,
 threshold DOUBLE PRECISION NOT NULL,
 predicted_high BOOLEAN NOT NULL,
 actual_fault BOOLEAN NOT NULL,
 fault_onset_ms BIGINT,
 label_source TEXT NOT NULL,
 label_confidence TEXT NOT NULL,
 labeled_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 labeled_by TEXT,
 note TEXT
);
CREATE INDEX IF NOT EXISTS idx_prediction_outcomes_device_time ON prediction_outcomes(device_id, window_end_ms DESC);
CREATE INDEX IF NOT EXISTS idx_prediction_outcomes_window_end ON prediction_outcomes(window_end_ms);
CREATE INDEX IF NOT EXISTS idx_prediction_outcomes_labeled_at ON prediction_outcomes(labeled_at DESC);
CREATE TABLE IF NOT EXISTS prediction_quality_daily (
 day DATE PRIMARY KEY,
 model_version TEXT NOT NULL DEFAULT '',
 samples INT NOT NULL DEFAULT 0,
 tp INT NOT NULL DEFAULT 0,
 fp INT NOT NULL DEFAULT 0,
 fn INT NOT NULL DEFAULT 0,
 tn INT NOT NULL DEFAULT 0,
 precision DOUBLE PRECISION,
 recall DOUBLE PRECISION,
 f1 DOUBLE PRECISION,
 accuracy DOUBLE PRECISION,
 ece DOUBLE PRECISION,
 prob_mean DOUBLE PRECISION,
 high_ratio DOUBLE PRECISION,
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS model_registry (
 id BIGSERIAL PRIMARY KEY,
 model_version TEXT UNIQUE NOT NULL,
 algorithm TEXT NOT NULL DEFAULT 'xgboost',
 status TEXT NOT NULL DEFAULT 'candidate',
 threshold DOUBLE PRECISION,
 calibration JSONB,
 metrics JSONB,
 baseline_metrics JSONB,
 data_source TEXT,
 train_samples INT,
 positive_samples INT,
 artifact_path TEXT,
 trained_at TIMESTAMPTZ,
 promoted_at TIMESTAMPTZ,
 promoted_by TEXT,
 rejected_reason TEXT,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS prediction_drift_events (
 id BIGSERIAL PRIMARY KEY,
 detected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 level TEXT NOT NULL,
 signal TEXT NOT NULL,
 observed DOUBLE PRECISION,
 baseline DOUBLE PRECISION,
 threshold DOUBLE PRECISION,
 action TEXT NOT NULL,
 detail JSONB
);
CREATE INDEX IF NOT EXISTS idx_prediction_drift_events_detected_at ON prediction_drift_events(detected_at DESC);
