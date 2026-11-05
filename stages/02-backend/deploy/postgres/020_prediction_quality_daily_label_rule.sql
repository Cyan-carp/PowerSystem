-- 预测质量日表增加规则版本：让日指标与明细同一口径。
--
-- 背景：旧代码写入 prediction_quality_daily 时不带规则标记，1 小时口径切换后，
-- 这些历史日行（由 legacy/2h 明细算出）仍会被 series / detectMetricDrift 直接读取，
-- 导致“旧口径日数据混入新口径图表/漂移基线”。本迁移为日表补 label_rule，
-- 默认值 legacy（存量行保持旧口径，不冒充新口径）；新聚合显式写 window_1h。
-- 消费端（series、detectMetricDrift）只读取 label_rule='window_1h'，
-- 当日若无新 window_1h 数据，旧 legacy 日行不会被误读进新口径。

ALTER TABLE prediction_quality_daily
  ADD COLUMN IF NOT EXISTS label_rule TEXT NOT NULL DEFAULT 'legacy';

-- 原主键为 day 单列；新口径（window_1h）与存量口径（legacy）需同天并存，
-- 故改复合主键 (day, label_rule)，让新旧口径日行互不覆盖。
--
-- store.Migrate 没有版本表，每次 api 启动都会重跑本文件，因此下面三条必须可重复执行：
--   1. 先按 ctid 收敛重复行（正常路径下 0 行），避免 ADD PRIMARY KEY 因重复数据失败
--      而让 api 在启动阶段 logger.Fatal；
--   2. DROP 与 ADD 注册在同一事务内（见 store.atomicMigrations），不会出现“表暂时没有主键”
--      且并发写入插入重复行的窗口；
--   3. 主键与索引每次启动会重建一次，代价与表行数成正比——该表按 (日期 × 规则) 增长，
--      当前十几行、长期千行量级，毫秒级可接受。若将来改为按模型版本分表，这里要改成条件式迁移。
DELETE FROM prediction_quality_daily a
 USING prediction_quality_daily b
 WHERE a.ctid > b.ctid
   AND a.day = b.day
   AND a.label_rule = b.label_rule;

ALTER TABLE prediction_quality_daily DROP CONSTRAINT IF EXISTS prediction_quality_daily_pkey;
ALTER TABLE prediction_quality_daily ADD PRIMARY KEY (day, label_rule);

CREATE INDEX IF NOT EXISTS idx_prediction_quality_daily_rule_day
  ON prediction_quality_daily(label_rule, day);
