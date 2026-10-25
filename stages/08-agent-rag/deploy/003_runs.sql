-- S03 additive migration; executed only through Store.open.
ALTER TABLE rag_sessions ADD COLUMN IF NOT EXISTS deleted_at timestamptz;
CREATE UNIQUE INDEX IF NOT EXISTS rag_sessions_owner_key ON rag_sessions(id,user_id);
CREATE UNIQUE INDEX IF NOT EXISTS rag_turns_session_key ON rag_turns(id,session_id);
CREATE TABLE IF NOT EXISTS assistant_runs (
 id uuid PRIMARY KEY, user_id bigint NOT NULL, session_id uuid NOT NULL,
 turn_id bigint NOT NULL UNIQUE, idempotency_key uuid NOT NULL, request_hash text NOT NULL,
 request jsonb NOT NULL, context_snapshot jsonb NOT NULL, model_profile jsonb NOT NULL,
 status text NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','awaiting_approval','reconciling','succeeded','partial','failed','cancelled')),
 phase text NOT NULL DEFAULT 'queued', final_answer text NOT NULL DEFAULT '', answer_version bigint NOT NULL DEFAULT 0,
 result_refs jsonb NOT NULL DEFAULT '[]', actions jsonb NOT NULL DEFAULT '[]', artifacts jsonb NOT NULL DEFAULT '[]',
 terminal_event jsonb, last_seq bigint NOT NULL DEFAULT 0, error jsonb,
 cancel_requested boolean NOT NULL DEFAULT false, requires_manual_reconciliation boolean NOT NULL DEFAULT false,
 deadline timestamptz NOT NULL, lease_owner uuid, lease_until timestamptz, lease_epoch bigint NOT NULL DEFAULT 0,
 auth_sealed bytea NOT NULL, reconciliation_deadline timestamptz, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(user_id,idempotency_key),
 FOREIGN KEY(session_id,user_id) REFERENCES rag_sessions(id,user_id),
 FOREIGN KEY(turn_id,session_id) REFERENCES rag_turns(id,session_id)
);
ALTER TABLE assistant_runs ADD COLUMN IF NOT EXISTS reconciliation_deadline timestamptz;
ALTER TABLE assistant_runs ADD COLUMN IF NOT EXISTS auth_actor jsonb;
CREATE INDEX IF NOT EXISTS assistant_runs_owner_session ON assistant_runs(user_id,session_id,created_at);
CREATE INDEX IF NOT EXISTS assistant_runs_queue ON assistant_runs(status,lease_until);
ALTER TABLE rag_turns ADD COLUMN IF NOT EXISTS run_id uuid REFERENCES assistant_runs(id);
CREATE UNIQUE INDEX IF NOT EXISTS rag_turns_run ON rag_turns(run_id) WHERE run_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS assistant_run_events (
 run_id uuid NOT NULL REFERENCES assistant_runs(id), seq bigint NOT NULL,
 type text NOT NULL, at timestamptz NOT NULL DEFAULT now(), data jsonb NOT NULL,
 PRIMARY KEY(run_id,seq)
);
CREATE TABLE IF NOT EXISTS assistant_run_outbox (
 run_id uuid PRIMARY KEY REFERENCES assistant_runs(id), available_at timestamptz NOT NULL DEFAULT now(), delivered_at timestamptz
);
ALTER TABLE assistant_run_outbox ADD COLUMN IF NOT EXISTS cancel_children boolean NOT NULL DEFAULT false;
ALTER TABLE assistant_run_outbox ADD COLUMN IF NOT EXISTS cancel_retry_at timestamptz NOT NULL DEFAULT now();
-- Model/tool step journal closes the gap between completed external calls and graph checkpoints.
CREATE TABLE IF NOT EXISTS assistant_run_steps (
 run_id uuid NOT NULL REFERENCES assistant_runs(id), step_key text NOT NULL, kind text NOT NULL,
 request_hash text NOT NULL, state text NOT NULL CHECK(state IN ('started','completed')),
 attempts integer NOT NULL DEFAULT 1, output jsonb, result_state jsonb,
 PRIMARY KEY(run_id,step_key)
);
-- Independent LangGraph namespace, thread_id is the run UUID.
CREATE TABLE IF NOT EXISTS assistant_checkpoints (
 run_id uuid NOT NULL REFERENCES assistant_runs(id), ns text NOT NULL, checkpoint_id text NOT NULL,
 parent_id text, checkpoint_type text NOT NULL, checkpoint bytea NOT NULL, metadata jsonb NOT NULL,
 PRIMARY KEY(run_id,ns,checkpoint_id)
);
CREATE TABLE IF NOT EXISTS assistant_checkpoint_writes (
 run_id uuid NOT NULL REFERENCES assistant_runs(id), ns text NOT NULL, checkpoint_id text NOT NULL,
 task_id text NOT NULL, idx integer NOT NULL, channel text NOT NULL, value_type text NOT NULL, value bytea NOT NULL,
 PRIMARY KEY(run_id,ns,checkpoint_id,task_id,idx)
);
-- Projection only; stage9 remains the approval and execution authority.
CREATE TABLE IF NOT EXISTS assistant_actions (
 action_id uuid PRIMARY KEY, run_id uuid NOT NULL REFERENCES assistant_runs(id), user_id bigint NOT NULL,
 authority_revision bigint NOT NULL, observed_at timestamptz NOT NULL, projection jsonb NOT NULL
);
