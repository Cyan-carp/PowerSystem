-- S05 additive migration. File bytes remain outside PostgreSQL and model context.
CREATE TABLE IF NOT EXISTS assistant_artifact_batches (
 run_id uuid NOT NULL REFERENCES assistant_runs(id), call_id text NOT NULL,
 request_hash text NOT NULL, status text NOT NULL CHECK(status IN ('ready','failed')),
 summary jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(run_id,call_id)
);
CREATE TABLE IF NOT EXISTS assistant_artifacts (
 id uuid PRIMARY KEY, run_id uuid NOT NULL REFERENCES assistant_runs(id),
 user_id bigint NOT NULL, call_id text NOT NULL, name text NOT NULL,
 mime_type text NOT NULL, storage_key text NOT NULL UNIQUE, size bigint NOT NULL,
 sha256 text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 expires_at timestamptz NOT NULL DEFAULT now()+interval '7 days',
 status text NOT NULL CHECK(status IN ('ready','expired','unavailable')),
 provenance jsonb NOT NULL, UNIQUE(run_id,call_id,mime_type)
);
CREATE INDEX IF NOT EXISTS assistant_artifacts_owner_run ON assistant_artifacts(user_id,run_id);
ALTER TABLE assistant_artifacts ADD COLUMN IF NOT EXISTS file_removed_at timestamptz;
CREATE INDEX IF NOT EXISTS assistant_artifacts_expiry ON assistant_artifacts(expires_at) WHERE status<>'expired';
CREATE INDEX IF NOT EXISTS assistant_artifacts_cleanup ON assistant_artifacts(expires_at) WHERE status='expired' AND file_removed_at IS NULL;
CREATE TABLE IF NOT EXISTS assistant_artifact_downloads (
 id bigserial PRIMARY KEY, artifact_id uuid NOT NULL, user_id bigint NOT NULL,
 outcome text NOT NULL, at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE assistant_artifact_downloads ADD COLUMN IF NOT EXISTS run_id uuid;
ALTER TABLE assistant_artifact_downloads ADD COLUMN IF NOT EXISTS byte_count bigint NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS assistant_artifact_attempts (
 id bigserial PRIMARY KEY, run_id uuid NOT NULL REFERENCES assistant_runs(id),
 call_id text NOT NULL, outcome text NOT NULL, at timestamptz NOT NULL DEFAULT now()
);
