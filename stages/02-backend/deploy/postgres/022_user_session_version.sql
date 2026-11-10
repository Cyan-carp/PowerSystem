-- JWT 会话版本：旧令牌未携带版本时按 0 处理，首次改密后失效。
-- Migrate 每次启动重跑，迁移必须可重复执行。
ALTER TABLE users ADD COLUMN IF NOT EXISTS session_version BIGINT NOT NULL DEFAULT 0;
