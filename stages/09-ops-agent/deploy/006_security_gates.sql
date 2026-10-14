-- Immutable SEC-M2 material. Network/model calls never run inside these transactions.
CREATE TABLE IF NOT EXISTS ma_security_gate_contexts (
 review_id uuid PRIMARY KEY, task_id uuid NOT NULL, step_no integer NOT NULL,
 execution_epoch bigint NOT NULL, user_id bigint NOT NULL, role text NOT NULL,
 kind text NOT NULL, material jsonb NOT NULL, material_hash text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 FOREIGN KEY(task_id,step_no) REFERENCES ma_task_steps(task_id,step_no)
);
CREATE INDEX IF NOT EXISTS ma_security_gate_step ON ma_security_gate_contexts(task_id,step_no,execution_epoch,kind,created_at);
CREATE TABLE IF NOT EXISTS ma_security_snapshot_claims (
 grant_hash text PRIMARY KEY, task_id uuid NOT NULL REFERENCES ma_tasks(task_id),
 execution_epoch bigint NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
