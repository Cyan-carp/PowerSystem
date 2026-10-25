-- S03 binding only: no new approval or execution authority.
ALTER TABLE ma_tasks ADD COLUMN IF NOT EXISTS run_id uuid;
CREATE INDEX IF NOT EXISTS ma_tasks_run ON ma_tasks(user_id,run_id) WHERE run_id IS NOT NULL;
