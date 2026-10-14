-- Explicit SEC-M1 controller migration; not loaded by the existing Operation Worker.
CREATE TABLE IF NOT EXISTS ma_security_reviews (
 review_id uuid PRIMARY KEY,
 task_id uuid NOT NULL REFERENCES ma_tasks(task_id), step_no integer NOT NULL,
 trace_id uuid NOT NULL, user_id bigint NOT NULL CHECK(user_id>0),
 execution_epoch bigint NOT NULL, kind text NOT NULL,
 request_hash text NOT NULL, binding_hash text NOT NULL,
 request jsonb NOT NULL,
 state text NOT NULL CHECK(state IN ('pending','completed','unavailable','invalid')),
 decision jsonb, decision_hash text, signature_validated boolean NOT NULL DEFAULT false,
 expires_at timestamptz, error_code text, usage jsonb NOT NULL DEFAULT '{}',
 duration_ms integer, model text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz,
 FOREIGN KEY(task_id,step_no) REFERENCES ma_task_steps(task_id,step_no)
);
CREATE INDEX IF NOT EXISTS ma_security_reviews_owner ON ma_security_reviews(user_id,created_at);
CREATE TABLE IF NOT EXISTS ma_security_review_events (
 event_seq bigserial PRIMARY KEY, review_id uuid NOT NULL REFERENCES ma_security_reviews(review_id),
 state text NOT NULL CHECK(state IN ('pending','completed','unavailable','invalid')),
 reason text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
