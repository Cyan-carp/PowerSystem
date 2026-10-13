CREATE TABLE IF NOT EXISTS ma_tasks (
 task_id uuid PRIMARY KEY, trace_id uuid NOT NULL, session_id uuid NOT NULL,
 user_id bigint NOT NULL CHECK(user_id>0), parent_turn_id bigint CHECK(parent_turn_id>0),
 idempotency_key uuid NOT NULL, request_hash text NOT NULL, envelope jsonb NOT NULL,
 status text NOT NULL CHECK(status IN ('queued','planning','executing','awaiting_approval','succeeded','failed','cancelled','timeout')),
 deadline timestamptz NOT NULL, execution_epoch bigint NOT NULL DEFAULT 0,
 worker_id text, lease_until timestamptz, cancel_requested boolean NOT NULL DEFAULT false,
 model_calls integer NOT NULL DEFAULT 0, result jsonb NOT NULL DEFAULT '{}',
 link_pending boolean NOT NULL DEFAULT true, signature_validated boolean NOT NULL DEFAULT true,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz,
 UNIQUE(user_id,idempotency_key)
);
CREATE INDEX IF NOT EXISTS ma_tasks_owner ON ma_tasks(user_id,created_at DESC,task_id DESC);
CREATE INDEX IF NOT EXISTS ma_tasks_session ON ma_tasks(session_id,status);
CREATE INDEX IF NOT EXISTS ma_tasks_lease ON ma_tasks(status,lease_until);
CREATE TABLE IF NOT EXISTS ma_task_steps (
 task_id uuid NOT NULL REFERENCES ma_tasks(task_id), step_no integer NOT NULL,
 tool text NOT NULL, version text NOT NULL DEFAULT '1.0', args jsonb NOT NULL, args_hash text NOT NULL,
 risk_class text NOT NULL DEFAULT 'R0' CHECK(risk_class='R0'), required boolean NOT NULL DEFAULT true,
 status text NOT NULL CHECK(status IN ('pending','running','succeeded','failed','skipped','cancelled','timeout')),
 execution_epoch bigint NOT NULL, result jsonb, error jsonb,
 started_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz,
 PRIMARY KEY(task_id,step_no)
);
CREATE TABLE IF NOT EXISTS ma_tool_calls (
 call_id uuid PRIMARY KEY, task_id uuid NOT NULL, step_no integer NOT NULL,
 trace_id uuid NOT NULL, execution_epoch bigint NOT NULL, args_hash text NOT NULL,
 status text NOT NULL CHECK(status IN ('running','succeeded','failed','cancelled','timeout')),
 data jsonb, error jsonb, duration_ms integer, collected_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz,
 FOREIGN KEY(task_id,step_no) REFERENCES ma_task_steps(task_id,step_no)
);
CREATE TABLE IF NOT EXISTS ma_task_events (
 event_seq bigserial PRIMARY KEY, task_id uuid NOT NULL REFERENCES ma_tasks(task_id),
 from_status text, to_status text NOT NULL, execution_epoch bigint NOT NULL, reason text,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS ma_outbox (
 event_id bigserial PRIMARY KEY, task_id uuid NOT NULL REFERENCES ma_tasks(task_id),
 delivered boolean NOT NULL DEFAULT false, attempts integer NOT NULL DEFAULT 0,
 next_attempt_at timestamptz NOT NULL DEFAULT now(), last_error text
);
CREATE INDEX IF NOT EXISTS ma_outbox_pending ON ma_outbox(next_attempt_at) WHERE NOT delivered;
CREATE TABLE IF NOT EXISTS ma_session_guards (
 session_id uuid PRIMARY KEY, user_id bigint NOT NULL, operation_id uuid NOT NULL,
 state text NOT NULL CHECK(state IN ('reserved','deleted')),
 created_at timestamptz NOT NULL DEFAULT now()
);
