-- 007_alarm_closure.sql
-- 前置：001_init.sql 已创建 alarm_records、alarm_rules（数据库基线 v3.0.0 之后）。
-- 目的：区分「告警恢复」与「管理关闭」，并保存触发时的规则快照。
-- 执行：由 cmd/api/main.go 在既有迁移之后顺序追加；失败停止，不回退已应用的前序迁移。
-- 兼容：全部新列可空或带默认值，旧记录保持 NULL，不推断补填。

ALTER TABLE alarm_records ADD COLUMN IF NOT EXISTS close_reason VARCHAR(32);
ALTER TABLE alarm_records ADD COLUMN IF NOT EXISTS rule_operator VARCHAR(2);
ALTER TABLE alarm_records ADD COLUMN IF NOT EXISTS rule_version VARCHAR(64);

-- 规则版本：新增列，默认 v1，用于触发时快照；旧规则统一记为 v1（基线，非推断其历史配置）。
ALTER TABLE alarm_rules ADD COLUMN IF NOT EXISTS version VARCHAR(64) NOT NULL DEFAULT 'v1';

-- close_reason 允许值：
--   telemetry_recovered   遥测不再越限，规则判定恢复
--   prediction_risk_resolved 预测风险下降为低，AI 告警恢复
--   rule_disabled         规则被禁用导致告警关闭
--   rule_deleted          规则被删除导致告警关闭
--   device_deleted        设备被删除导致告警关闭
--   历史记录为 NULL（视为 unknown），不推断补填。

-- 仅对历史已恢复但无关闭原因的记录补一层可判读标记，不改动任何时间与状态。
UPDATE alarm_records SET close_reason = 'unknown'
 WHERE status = 'recovered' AND close_reason IS NULL;

COMMENT ON COLUMN alarm_records.close_reason IS '告警结束原因：遥测恢复/预测风险下降/规则禁用/规则删除/设备删除/unknown';
COMMENT ON COLUMN alarm_records.rule_operator IS '触发时的规则比较运算符快照，历史缺失为 NULL';
COMMENT ON COLUMN alarm_records.rule_version IS '触发时适用的规则版本快照，历史缺失为 NULL';
