-- 任务记录软删除：行保留供审计追溯，只有列表查询会隐藏。
-- 依赖 001_tasks.sql 已建表；全部 IF NOT EXISTS，Store.open 每次启动重跑安全。
ALTER TABLE ma_tasks ADD COLUMN IF NOT EXISTS deleted_at timestamptz;
ALTER TABLE ma_tasks ADD COLUMN IF NOT EXISTS deleted_by bigint;
COMMENT ON COLUMN ma_tasks.deleted_at IS '用户主动从任务记录移除的时间；非物理删除';
COMMENT ON COLUMN ma_tasks.deleted_by IS '执行移除的用户ID；仅审计用，不建外键';
