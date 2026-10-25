ALTER TABLE ma_task_steps DROP CONSTRAINT IF EXISTS ma_task_steps_risk_class_check;
ALTER TABLE ma_task_steps ADD CONSTRAINT ma_task_steps_risk_class_check CHECK(risk_class IN ('R0','R1','R2'));
CREATE TABLE IF NOT EXISTS ma_policy_versions (
 version text PRIMARY KEY, snapshot jsonb NOT NULL, activated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS ma_approvals (
 approval_id uuid PRIMARY KEY, task_id uuid NOT NULL REFERENCES ma_tasks(task_id), step_no integer NOT NULL,
 plan jsonb NOT NULL, args_hash text NOT NULL, policy_version text NOT NULL REFERENCES ma_policy_versions(version),
 status text NOT NULL CHECK(status IN ('pending','approved','rejected','expired','invalidated','cancelled')),
 created_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz NOT NULL,
 decided_at timestamptz, consumed_at timestamptz,
 FOREIGN KEY(task_id,step_no) REFERENCES ma_task_steps(task_id,step_no), UNIQUE(task_id,step_no)
);
CREATE TABLE IF NOT EXISTS ma_execution_attempts (
 attempt_id uuid PRIMARY KEY, task_id uuid NOT NULL REFERENCES ma_tasks(task_id), step_no integer NOT NULL,
 approval_id uuid NOT NULL UNIQUE REFERENCES ma_approvals(approval_id),
 call_id uuid NOT NULL UNIQUE REFERENCES ma_tool_calls(call_id), execution_epoch bigint NOT NULL,
 status text NOT NULL CHECK(status IN ('started','succeeded','failed','execution_uncertain')),
 started_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz, evidence jsonb,
 UNIQUE(task_id,step_no)
);
CREATE INDEX IF NOT EXISTS ma_approvals_pending ON ma_approvals(expires_at) WHERE status IN ('pending','approved');
