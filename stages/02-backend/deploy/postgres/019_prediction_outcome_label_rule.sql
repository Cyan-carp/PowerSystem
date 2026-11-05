-- 预测质量真值规则版本与历史修正审计。
--
-- 背景：2026-11-03 的 b34ee66 把 windowFault 右端从 +1h 放宽到 +2h，
-- 但训练目标和正式真值契约仍是 (window_end, window_end+1h]。旧行无法与新行区分，
-- 因此新增 label_rule：旧行保持 legacy，1 小时口径显式写 window_1h。
-- 注意：默认值故意保持 legacy。即使旧 api 在迁移后继续写入，也不会被误标成新口径。

ALTER TABLE prediction_outcomes
  ADD COLUMN IF NOT EXISTS label_rule TEXT NOT NULL DEFAULT 'legacy';

CREATE INDEX IF NOT EXISTS idx_prediction_outcomes_rule_window
  ON prediction_outcomes(label_rule, window_end_ms);

-- 每次显式修正一行，用于追溯旧值、计数和失败可见性。
CREATE TABLE IF NOT EXISTS prediction_outcome_label_correction_runs (
  run_id TEXT PRIMARY KEY,
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ,
  actor TEXT NOT NULL,
  reason TEXT NOT NULL,
  epoch_ms BIGINT NOT NULL,
  mature_before_ms BIGINT NOT NULL,
  scanned INT NOT NULL DEFAULT 0,
  updated INT NOT NULL DEFAULT 0,
  changed INT NOT NULL DEFAULT 0,
  unchanged INT NOT NULL DEFAULT 0,
  flipped_positive_to_negative INT NOT NULL DEFAULT 0,
  flipped_negative_to_positive INT NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'running',
  error TEXT
);

CREATE TABLE IF NOT EXISTS prediction_outcome_label_corrections (
  id BIGSERIAL PRIMARY KEY,
  prediction_id BIGINT NOT NULL,
  run_id TEXT NOT NULL,
  rule_from TEXT NOT NULL,
  rule_to TEXT NOT NULL,
  actual_fault_before BOOLEAN NOT NULL,
  fault_onset_ms_before BIGINT,
  actual_fault_after BOOLEAN NOT NULL,
  fault_onset_ms_after BIGINT,
  changed BOOLEAN NOT NULL,
  corrected_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_prediction_outcome_label_corrections
  ON prediction_outcome_label_corrections(prediction_id, rule_to);

CREATE INDEX IF NOT EXISTS idx_prediction_outcome_label_corrections_run
  ON prediction_outcome_label_corrections(run_id);
