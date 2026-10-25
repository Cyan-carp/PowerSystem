-- S08 config grants are read-only scope; ma_approvals remains the action authority.
CREATE TABLE IF NOT EXISTS assistant_schedules (
 id uuid PRIMARY KEY, user_id bigint NOT NULL, config_version bigint NOT NULL CHECK(config_version>0),
 config jsonb NOT NULL, scope_hash text NOT NULL, actor_role text NOT NULL,
 status text NOT NULL CHECK(status IN ('active','paused','cancelled')),
 next_run_at timestamptz, auth_sealed bytea NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(), reason text
);
CREATE INDEX IF NOT EXISTS assistant_schedules_due ON assistant_schedules(next_run_at) WHERE status='active';
CREATE TABLE IF NOT EXISTS assistant_schedule_grants (
 grant_id uuid PRIMARY KEY, schedule_id uuid NOT NULL REFERENCES assistant_schedules(id),
 user_id bigint NOT NULL, config_version bigint NOT NULL, scope_hash text NOT NULL,
 destination_id text NOT NULL CHECK(destination_id='platform_client'), destination_version text NOT NULL,
 approved_by bigint NOT NULL, approved_at timestamptz NOT NULL, expires_at timestamptz NOT NULL,
 revoked_at timestamptz, action_id uuid NOT NULL UNIQUE, execution_ref uuid NOT NULL UNIQUE,
 CHECK(expires_at>approved_at AND expires_at<=approved_at+interval '90 days'), CHECK(approved_by=user_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS assistant_schedule_one_grant ON assistant_schedule_grants(schedule_id) WHERE revoked_at IS NULL;
CREATE TABLE IF NOT EXISTS assistant_schedule_slots (
 schedule_id uuid NOT NULL REFERENCES assistant_schedules(id), scheduled_for timestamptz NOT NULL,
 grant_id uuid NOT NULL REFERENCES assistant_schedule_grants(grant_id), config_version bigint NOT NULL,
 run_id uuid UNIQUE REFERENCES assistant_runs(id), status text NOT NULL,
 reason text, skipped_count bigint NOT NULL DEFAULT 0, observed_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(schedule_id,scheduled_for)
);
CREATE TABLE IF NOT EXISTS assistant_schedule_requests (
 run_id uuid PRIMARY KEY REFERENCES assistant_runs(id), kind text NOT NULL CHECK(kind IN ('config','preview')),
 capability text NOT NULL, args jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS assistant_schedule_receipts (
 execution_ref uuid PRIMARY KEY, action_id uuid NOT NULL UNIQUE, user_id bigint NOT NULL,
 receipt jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS assistant_schedule_events (
 id bigserial PRIMARY KEY, schedule_id uuid, user_id bigint NOT NULL, kind text NOT NULL,
 detail jsonb NOT NULL DEFAULT '{}', created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS assistant_schedule_owner ON assistant_schedules(user_id,created_at);
