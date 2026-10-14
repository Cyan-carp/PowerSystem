-- Nullable legacy context: no retrospective permission grants.
ALTER TABLE ma_tasks ADD COLUMN IF NOT EXISTS runtime_context jsonb;
