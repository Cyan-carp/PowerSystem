CREATE TABLE IF NOT EXISTS agent_scan_cursor (
    name TEXT PRIMARY KEY,
    baseline_id BIGINT NOT NULL,
    last_id BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_monitor_events (
    event_key TEXT PRIMARY KEY,
    level TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    recovered_at TIMESTAMPTZ,
    payload JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS agent_interpretations (
    id BIGSERIAL PRIMARY KEY,
    event_key TEXT UNIQUE NOT NULL,
    category TEXT NOT NULL,
    alarm_id BIGINT REFERENCES alarm_records(id),
    level TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    task_status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_until TIMESTAMPTZ,
    lease_token TEXT,
    evidence JSONB,
    result JSONB,
    reason TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS agent_interpretations_pending ON agent_interpretations(task_status,next_attempt_at);
CREATE INDEX IF NOT EXISTS agent_interpretations_updated ON agent_interpretations(updated_at,id);
CREATE TABLE IF NOT EXISTS agent_interpretation_reads (
    interpretation_id BIGINT REFERENCES agent_interpretations(id) ON DELETE CASCADE,
    user_id BIGINT REFERENCES users(id) ON DELETE CASCADE,
    read_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(interpretation_id,user_id)
);
