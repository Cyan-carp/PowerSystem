ALTER TABLE ma_task_steps DROP CONSTRAINT IF EXISTS ma_task_steps_risk_class_check;
ALTER TABLE ma_task_steps ADD CONSTRAINT ma_task_steps_risk_class_check CHECK(risk_class IN ('R0','R1','R2'));
ALTER TABLE ma_approvals ADD COLUMN IF NOT EXISTS authority_revision bigint NOT NULL DEFAULT 1;
CREATE UNIQUE INDEX IF NOT EXISTS ma_business_action_binding ON ma_tasks ((envelope->>'action_id')) WHERE envelope->>'protocol'='ma-action/v1';
