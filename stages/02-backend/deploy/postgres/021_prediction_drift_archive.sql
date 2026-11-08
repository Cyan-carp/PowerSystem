-- 漂移事件软归档：与 008_alarm_archive.sql 同一套约定。
-- 只是打标记，不删除原始观察记录；默认列表只返回 archived = FALSE 的行。
-- Migrate 没有版本表、每次启动重跑，所以全部语句必须幂等。

ALTER TABLE prediction_drift_events
    ADD COLUMN IF NOT EXISTS archived BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE prediction_drift_events
    ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ;

ALTER TABLE prediction_drift_events
    ADD COLUMN IF NOT EXISTS archived_by TEXT;

-- 部分索引：列表默认只查未归档，分页按 (detected_at DESC, id DESC) 走这条就够
CREATE INDEX IF NOT EXISTS idx_prediction_drift_events_active
    ON prediction_drift_events (detected_at DESC, id DESC)
    WHERE archived = FALSE;

COMMENT ON COLUMN prediction_drift_events.archived IS '软归档标记；默认列表不返回已归档记录';
COMMENT ON COLUMN prediction_drift_events.archived_at IS '归档时间（UTC）';
COMMENT ON COLUMN prediction_drift_events.archived_by IS '归档操作人账号';
