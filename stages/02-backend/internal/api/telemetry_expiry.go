package api

import (
	"context"
	"errors"
	"math"
	"time"

	"gorm.io/gorm"
	"powersystem/backend/internal/service"
)

// IR-S02 时效出口。窗口与调度共用 inboxWindow / ts_ms 口径：超过 7 天仍未处理
// 的行不再有业务价值，写为 quarantined + window_expired 终态，而不是永远留在
// processed_at IS NULL 里把队龄指标顶高（IR-R01）。
//
// 冻结值按 96 历史接收 83,318 行/小时设计：2000 行 / 30 秒 = 240,000 行/小时，
// 约为历史接收速率的 2.9 倍，持续过期输入下才有净排水余量。修改这些值必须重新
// 给出隔离负载实测与预算证据，不能用单批毫秒数直接外推生产。
const (
	inboxExpiryInterval = 30 * time.Second
	inboxExpiryBatch    = 2000
	inboxExpiryBudget   = 5 * time.Second

	inboxQueueProbeInterval = 60 * time.Second
	inboxQueueProbeBudget   = 2 * time.Second
	// 队头观测按设备取值，上限只防设备表异常增长；当前平台 143 台。
	inboxHeadAgeDeviceLimit = 5000

	inboxExpiryFailureCode = "window_expired"
)

var errInboxExpiryPaused = errors.New("inbox_expiry_paused")

// inboxExpiryPredicate 与调度使用同一个 ts_ms 时钟（不能用 received_at）。
// S01-Q03：旧 ts、新接收的补传行只有按 ts_ms 才能被过期，否则 IR-R01 会复现。
const inboxExpiryPredicate = `processed_at IS NULL AND ts_ms<? AND failure_code IS DISTINCT FROM 'owner_abandoned'`

// inboxHeadAgeQuery 是 S01 冻结口径的队头分布：每台未删除设备取窗口内最旧
// pending 行（每设备一条），窗口外行不参与；不是“全局最旧 200 行”样本。
const inboxHeadAgeQuery = `WITH clock AS (SELECT ?::timestamptz AS now),
dev AS (
 SELECT id FROM devices WHERE deleted_at IS NULL ORDER BY id ASC LIMIT ?
),heads AS (
 SELECT h.received_at
 FROM dev CROSS JOIN LATERAL (
  SELECT i.received_at FROM telemetry_inbox i
  WHERE i.device_id=dev.id AND i.processed_at IS NULL AND i.ts_ms>=?
  ORDER BY i.id ASC LIMIT 1
 ) h
)
SELECT count(*) AS devices,
 count(*) FILTER (WHERE clock.now-received_at < interval '1 minute') AS lt_1m,
 count(*) FILTER (WHERE clock.now-received_at >= interval '1 minute' AND clock.now-received_at < interval '5 minutes') AS m1_5m,
 count(*) FILTER (WHERE clock.now-received_at >= interval '5 minutes' AND clock.now-received_at < interval '15 minutes') AS m5_15m,
 count(*) FILTER (WHERE clock.now-received_at >= interval '15 minutes' AND clock.now-received_at < interval '1 hour') AS m15_1h,
 count(*) FILTER (WHERE clock.now-received_at >= interval '1 hour' AND clock.now-received_at < interval '6 hours') AS m1_6h,
 count(*) FILTER (WHERE clock.now-received_at >= interval '6 hours') AS ge_6h
 FROM heads CROSS JOIN clock`

// buildInboxExpiryQuery 独立成纯函数，测试可直接断言谓词、方向、批量和终态字段，
// 不需要数据库。SELECT 与 UPDATE 都带 owner_abandoned 防护：SELECT 防止选中，
// UPDATE 防止在锁定后被其它路径改成保留原因。真实过期写入与通知 outbox
// 同一事务提交：不能只悄悄隔离而让所有者不知道有样本未正常入库。
//
// history_enabled=false 是操作者的历史处理暂停开关（README/016 的现有合同），
// 过期写入也必须 fail-closed：SELECT 与 UPDATE 都在同一语句内要求调度单例仍为
// true；门禁行缺失时同样没有候选。事务内的预读只负责把“暂停”与“维护跳过”
// 区分成不同指标，真正的写入门禁仍在 SQL 内。
func buildInboxExpiryQuery() string {
	return `WITH gate AS (
 SELECT 1 AS ok FROM telemetry_scheduler WHERE id=1 AND history_enabled
), expired AS (
 SELECT id FROM telemetry_inbox WHERE ` + inboxExpiryPredicate + ` AND EXISTS (SELECT 1 FROM gate) ORDER BY id ASC LIMIT ? FOR UPDATE SKIP LOCKED
), updated AS (
 UPDATE telemetry_inbox AS i SET business_state='quarantined',failure_code='` + inboxExpiryFailureCode + `',processed_at=clock_timestamp(),next_attempt_at=NULL
 FROM expired WHERE i.id=expired.id AND i.processed_at IS NULL AND i.failure_code IS DISTINCT FROM 'owner_abandoned' AND EXISTS (SELECT 1 FROM gate)
 RETURNING i.id,i.run_id
), summary AS (
 SELECT count(*) AS changed_rows,
  count(*) FILTER (WHERE coalesce(left(run_id,13)='ir_s02_probe_',false)) AS probe_rows
 FROM updated
), notice_groups AS (
 SELECT coalesce(left(run_id,13)='ir_s02_probe_',false) AS test_only,
  count(*) AS changed_rows,min(id) AS first_id,max(id) AS last_id
 FROM updated GROUP BY coalesce(left(run_id,13)='ir_s02_probe_',false)
), notice_clock AS MATERIALIZED (
 SELECT clock_timestamp() AS at
), notice_event AS (
 SELECT (CASE WHEN notice_groups.test_only THEN 'inbox-expiry-test:' ELSE 'inbox-expiry:' END)||to_char(
   to_timestamp(floor(extract(epoch FROM notice_clock.at)/900)*900) AT TIME ZONE 'UTC',
   'YYYYMMDDHH24MI') AS event_id,
   notice_groups.changed_rows,notice_groups.first_id,notice_groups.last_id,
   notice_groups.test_only,notice_clock.at
 FROM notice_groups CROSS JOIN notice_clock
), notice AS (
 INSERT INTO business_notification_outbox(event_key,payload)
 SELECT event_id,jsonb_build_object(
  'event_id',event_id,'category','inbox_expiry','state','quarantined',
  'rows',changed_rows,'first_inbox_id',first_id,'last_inbox_id',last_id,
  'test_only',test_only,
  'occurred_at',to_char(at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS"Z"'))
 FROM notice_event WHERE true ON CONFLICT(event_key) DO NOTHING RETURNING id
) SELECT summary.changed_rows,summary.probe_rows FROM summary CROSS JOIN (SELECT count(*) FROM notice) AS notices`
}

type inboxExpirySettings struct {
	Enabled  bool
	Interval time.Duration
	Batch    int
	Budget   time.Duration
}

func (s inboxExpirySettings) withDefaults() inboxExpirySettings {
	if s.Interval <= 0 {
		s.Interval = inboxExpiryInterval
	}
	if s.Batch <= 0 {
		s.Batch = inboxExpiryBatch
	}
	if s.Budget <= 0 {
		s.Budget = inboxExpiryBudget
	}
	return s
}

// expireInboxBatch 只在一个有界事务里处理一批。history_enabled 与维护门禁都在
// 事务内检查：调度单例缺失或暂停时返回 errInboxExpiryPaused，不写终态。
func (s *Server) expireInboxBatchDetails(ctx context.Context, floor int64, limit int) (int64, int64, error) {
	var result struct {
		ChangedRows int64
		ProbeRows   int64
	}
	err := service.TelemetryTransaction(s.db.WithContext(ctx), func(tx *gorm.DB) error {
		var gate struct{ HistoryEnabled bool }
		res := tx.Raw(`SELECT history_enabled FROM telemetry_scheduler WHERE id=1`).Scan(&gate)
		if res.Error != nil {
			return res.Error
		}
		if res.RowsAffected != 1 || !gate.HistoryEnabled {
			return errInboxExpiryPaused
		}
		return tx.Raw(buildInboxExpiryQuery(), floor, limit).Scan(&result).Error
	})
	if err != nil {
		return 0, 0, err
	}
	return result.ChangedRows, result.ProbeRows, nil
}

func (s *Server) expireInboxBatch(ctx context.Context, floor int64, limit int) (int64, error) {
	changed, _, err := s.expireInboxBatchDetails(ctx, floor, limit)
	return changed, err
}

// expireInboxWindow 是单 worker 的一轮清理。它不再持有 scheduler.mu：
// 过期集合是 ts_ms < floor，实时入库只取 ts_ms >= floor，两者按定义不相交；
// 行级 FOR UPDATE SKIP LOCKED 再保证与正在入库的单行不互相等待。共享锁会让
// 每轮清理直接停住实时 tick，而历史净余量只有约 0.92%，不能用维护清理去换。
func (s *Server) expireInboxWindow(parent context.Context) {
	started := time.Now()
	defer func() { s.telemetry.elapsed("expiry", time.Since(started)) }()

	settings := s.expiry.withDefaults()
	ctx, cancel := context.WithTimeout(parent, settings.Budget)
	defer cancel()

	floor := time.Now().UTC().Add(-inboxWindow).UnixMilli()
	changed, probeRows, err := s.expireInboxBatchDetails(ctx, floor, settings.Batch)
	now := time.Now()
	s.telemetry.add("expiry_run", 1, now)
	if err != nil {
		switch {
		case errors.Is(err, errInboxExpiryPaused):
			s.telemetry.add("expiry_paused", 1, now)
		case errors.Is(err, service.ErrAlarmMaintenance), errors.Is(err, service.ErrMaintenanceMissing):
			s.telemetry.add("expiry_skip", 1, now)
		case errors.Is(err, service.ErrCommitUnknown):
			// 事务可能已提交也可能没有；下一轮按 processed_at 幂等复核，不重放业务动作。
			s.telemetry.add("expiry_unknown", 1, now)
			s.telemetryLog("expiry_unknown", now)
		case ctx.Err() != nil:
			s.telemetry.add("expiry_timeout", 1, now)
			s.telemetryLog("expiry_timeout", now)
		default:
			s.telemetry.add("expiry_failure", 1, now)
			s.telemetryLog("expiry_failure", now)
		}
		return
	}
	s.telemetry.add("expiry_success", 1, now)
	if changed > 0 {
		s.telemetry.add("expiry_rows", uint64(changed), now)
		if probeRows > 0 {
			s.telemetry.add("expiry_probe_rows", uint64(probeRows), now)
		}
	}
}

func (s *Server) expiryWorker(ctx context.Context) {
	ticker := time.NewTicker(s.expiry.withDefaults().Interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			s.expireInboxWindow(ctx)
		}
	}
}

// unknownInboxQueueValues 是未知态。计数与分桶用 NaN 表示“没有可用测量”，
// stats_known=0 与 NaN 同时出现；不能把未知误读成 0 条。
func unknownInboxQueueValues() map[string]float64 {
	v := map[string]float64{}
	for _, key := range []string{
		"inbox_pending_rows_all",
		"inbox_pending_rows_in_window",
		"inbox_pending_rows_outside_window",
		"inbox_outside_window_oldest_age_seconds",
		"inbox_head_age_devices_total",
		"inbox_head_age_devices_lt_1m",
		"inbox_head_age_devices_1m_5m",
		"inbox_head_age_devices_5m_15m",
		"inbox_head_age_devices_15m_1h",
		"inbox_head_age_devices_1h_6h",
		"inbox_head_age_devices_ge_6h",
	} {
		v[key] = math.NaN()
	}
	v["inbox_queue_stats_known"] = 0
	v["inbox_head_age_device_limit"] = float64(inboxHeadAgeDeviceLimit)
	return v
}

// inboxQueueStats 是低成本的队列观测：
//   - 三个 pending 计数来自同一条语句、同一快照，是精确值；
//   - outside_window_oldest_age 是该快照里最老的窗口外 pending 行，没有则为 NaN；
//   - 队头分桶按 S01 口径：每台未删除设备取窗口内（ts_ms>=floor）最旧 pending
//     行，按该行 received_at 的年龄分桶。每台设备最多贡献一条，窗口外行不参与，
//     不会把同一台设备的多条旧行重复计入。
//
// 查询按 device 表分页并逐设备 LATERAL LIMIT 1，受设备上限和 2 秒预算约束；
// 它仍可能为跳过窗口外旧行而扫描每台设备的 pending 前缀，生产是否可接受必须
// 用真实数据 EXPLAIN 证明，不能用隔离小表的外推。
func (s *Server) inboxQueueStats(ctx context.Context, now time.Time, deviceLimit int) (map[string]float64, error) {
	if deviceLimit <= 0 {
		deviceLimit = inboxHeadAgeDeviceLimit
	}
	floor := now.UTC().Add(-inboxWindow).UnixMilli()
	var counts struct {
		AllRows       int64
		InWindow      int64
		OutsideWindow int64
		OutsideOldest *time.Time
	}
	if err := s.db.WithContext(ctx).Raw(`SELECT count(*) AS all_rows,
 count(*) FILTER (WHERE ts_ms>=?) AS in_window,
 count(*) FILTER (WHERE ts_ms<?) AS outside_window,
 min(received_at) FILTER (WHERE ts_ms<?) AS outside_oldest
 FROM telemetry_inbox WHERE processed_at IS NULL`, floor, floor, floor).Scan(&counts).Error; err != nil {
		return nil, err
	}
	var buckets struct {
		Devices  int64
		Lt1m     int64 `gorm:"column:lt_1m"`
		M1to5m   int64 `gorm:"column:m1_5m"`
		M5to15m  int64 `gorm:"column:m5_15m"`
		M15to60m int64 `gorm:"column:m15_1h"`
		M1to6h   int64 `gorm:"column:m1_6h"`
		Ge6h     int64 `gorm:"column:ge_6h"`
	}
	if err := s.db.WithContext(ctx).Raw(inboxHeadAgeQuery, now, deviceLimit, floor).Scan(&buckets).Error; err != nil {
		return nil, err
	}
	v := unknownInboxQueueValues()
	v["inbox_pending_rows_all"] = float64(counts.AllRows)
	v["inbox_pending_rows_in_window"] = float64(counts.InWindow)
	v["inbox_pending_rows_outside_window"] = float64(counts.OutsideWindow)
	if counts.OutsideOldest != nil {
		v["inbox_outside_window_oldest_age_seconds"] = math.Max(0, now.Sub(*counts.OutsideOldest).Seconds())
	}
	v["inbox_head_age_devices_total"] = float64(buckets.Devices)
	v["inbox_head_age_devices_lt_1m"] = float64(buckets.Lt1m)
	v["inbox_head_age_devices_1m_5m"] = float64(buckets.M1to5m)
	v["inbox_head_age_devices_5m_15m"] = float64(buckets.M5to15m)
	v["inbox_head_age_devices_15m_1h"] = float64(buckets.M15to60m)
	v["inbox_head_age_devices_1h_6h"] = float64(buckets.M1to6h)
	v["inbox_head_age_devices_ge_6h"] = float64(buckets.Ge6h)
	v["inbox_head_age_device_limit"] = float64(deviceLimit)
	v["inbox_queue_stats_known"] = 1
	return v, nil
}

func (s *Server) probeInboxQueue(parent context.Context) {
	now := time.Now()
	ctx, cancel := context.WithTimeout(parent, inboxQueueProbeBudget)
	defer cancel()
	values, err := s.inboxQueueStats(ctx, now, inboxHeadAgeDeviceLimit)
	if err != nil {
		values = unknownInboxQueueValues()
		s.telemetry.add("inbox_queue_probe_error", 1, now)
		s.telemetryLog("inbox_queue_probe", now)
	}
	s.telemetry.mu.Lock()
	s.telemetry.auxValues = values
	if s.telemetry.times == nil {
		s.telemetry.times = map[string]time.Time{}
	}
	s.telemetry.times["inbox_queue"] = now
	s.telemetry.mu.Unlock()
}

func (s *Server) inboxQueueWatcher(ctx context.Context) {
	ticker := time.NewTicker(inboxQueueProbeInterval)
	defer ticker.Stop()
	for {
		s.probeInboxQueue(ctx)
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}
