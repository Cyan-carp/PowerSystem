-- Additive: old results and event identities remain readable.
ALTER TABLE agent_interpretations ADD COLUMN IF NOT EXISTS protocol_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE agent_interpretations ADD COLUMN IF NOT EXISTS model_calls INTEGER NOT NULL DEFAULT 0 CHECK (model_calls >= 0);
-- Reservations survive process crashes. Never reset this count on recovery.
CREATE INDEX IF NOT EXISTS agent_interpretation_retry_deadline ON agent_interpretations(created_at)
 WHERE task_status IN ('pending','running');
