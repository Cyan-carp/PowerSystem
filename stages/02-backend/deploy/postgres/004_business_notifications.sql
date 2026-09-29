CREATE TABLE IF NOT EXISTS business_notification_outbox (
 id BIGSERIAL PRIMARY KEY,
 event_key VARCHAR(128) NOT NULL UNIQUE,
 payload JSONB NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0,
 next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 sent_at TIMESTAMPTZ,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_business_notification_pending
 ON business_notification_outbox(next_attempt_at,id) WHERE sent_at IS NULL;
