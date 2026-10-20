-- 008_alarm_archive.sql
-- 告警归档是软删除：只标记记录，不删除告警事实及其生命周期数据。

ALTER TABLE alarm_records
    ADD COLUMN IF NOT EXISTS archived BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_alarm_records_visible_triggered
    ON alarm_records(triggered_at DESC, id DESC)
    WHERE archived = FALSE;

COMMENT ON COLUMN alarm_records.archived IS '软删除/归档标记；默认列表不返回已归档记录';
