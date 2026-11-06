package api

import (
	"context"
	"errors"
	"sync"
	"time"

	"gorm.io/gorm"
	"powersystem/backend/internal/energy"
	"powersystem/backend/internal/model"
	"powersystem/backend/internal/service"
)

const inboxPageSize = 500

// inboxWindow 是入库调度的时间窗：比这更老的消息视为过期，不再参与调度。
// 它是「淘汰线之前的缓冲」，不是重试宽限——数据一行不删，payload 仍在表里，
// 只是不再被选中。窗口存在的意义是让长期积压自动出局，避免队头被永远好不了
// 的消息占住（2026-11-03 的死锁就是这么来的）。
const inboxWindow = 7 * 24 * time.Hour

// inboxPendingPredicate 同时用于取任务与事务内队头复核，两处必须一字不差。
// 2026-11-03 只改取任务方向、漏改复核，导致每条样本都被判「已不是队头」并走
// errInboxDeferred 静默返回，入库成功率归零 40 分钟。抽成常量是为了让两处无法漂移，
// 并由 TestInboxPredicateSharedByHeadsAndProbe 守住。
const inboxPendingPredicate = `processed_at IS NULL AND ts_ms>=?`

const tdMissingGrace = 10 * time.Minute
const tdMissingMinChecks = 6
const tdMissingMaxGap = 2 * time.Minute
const payloadGrace = 30 * time.Second

var errDependencyOpen = errors.New("telemetry_dependency_open")
var errInboxDeferred = errors.New("telemetry_inbox_deferred")

// Fixed workers/buffers/circuits and durable cursors. No per-device memory map.
type inboxScheduler struct{ mu sync.Mutex }
type dependencyCircuit struct {
	mu       sync.Mutex
	failures int
	until    time.Time
	probing  bool
}

func (b *dependencyCircuit) enter(now time.Time) bool {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.probing || now.Before(b.until) {
		return false
	}
	if b.failures >= 2 {
		b.probing = true
	}
	return true
}
func (b *dependencyCircuit) finish(now time.Time, err error, cancelled bool) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.probing = false
	if cancelled {
		return
	}
	if err == nil {
		b.failures = 0
		b.until = time.Time{}
		return
	}
	if b.failures < 32 {
		b.failures++
	}
	if b.failures >= 2 {
		b.until = now.Add(retryDelay(int64(b.failures-1), time.Second, 30*time.Second))
	}
}
func retryDelay(attempt int64, base, cap time.Duration) time.Duration {
	if attempt < 1 {
		attempt = 1
	}
	for n := int64(1); n < attempt && base < cap; n++ {
		base *= 2
	}
	if base > cap {
		return cap
	}
	return base
}

// Heads are selected before applying retry deadlines. Never let a later row
// bypass an unready head, even across run_id/seq resets.
//
// 每设备取**最旧**一条（id ASC），即先进先出。2026-11-03 曾因为积压时队头永远是
// 10-30 的旧消息、新样本永远排不到，临时改成「最新优先」（id DESC）。最新优先救活了
// 实时链路，但代价是每轮只取最新一条，中间的样本永远轮不到——实测会稳定留下
// 永久空洞（2026-11-06 界上 3,827 条未处理，平均每台 26 条，且仍在缓慢增长）。
//
// 现在改回先进先出，由 inboxWindow 兜底：超过 7 天的消息直接不参与调度，队头
// 不可能被「永远好不了」的旧消息占死，所以不会重演 11-03 的死锁。窗口与方向
// 是一对——去掉窗口就不能改回 ASC，改回 DESC 就会继续漏样本。
func (s *Server) inboxHeads(ctx context.Context, after, floor, through int64, limit int) ([]model.Inbox, error) {
	var rows []model.Inbox
	q, args := buildInboxHeadsQuery(after, floor, through, limit)
	err := s.db.WithContext(ctx).Raw(q, args...).Scan(&rows).Error
	return rows, err
}

// buildInboxHeadsQuery 独立成纯函数，让测试能直接断言「取任务」与「队头复核」
// 共享同一谓词、同一方向，而不必连数据库。
func buildInboxHeadsQuery(after, floor, through int64, limit int) (string, []any) {
	q := `WITH RECURSIVE heads AS ((SELECT * FROM telemetry_inbox WHERE ` + inboxPendingPredicate + ` AND device_id>?`
	args := []any{floor, after}
	if through > 0 {
		q += ` AND device_id<=?`
		args = append(args, through)
	}
	q += ` ORDER BY device_id,id ASC LIMIT 1) UNION ALL SELECT i.* FROM heads h CROSS JOIN LATERAL (SELECT * FROM telemetry_inbox WHERE ` + inboxPendingPredicate + ` AND device_id>h.device_id`
	args = append(args, floor)
	if through > 0 {
		q += ` AND device_id<=?`
		args = append(args, through)
	}
	q += ` ORDER BY device_id,id ASC LIMIT 1) i) SELECT * FROM heads LIMIT ?`
	args = append(args, limit)
	return q, args
}
func (s *Server) processPending(parent context.Context) {
	s.processPendingBudget(parent, inboxTickBudget)
}
func (s *Server) processPendingBudget(parent context.Context, budget time.Duration) {
	s.scheduler.mu.Lock()
	defer s.scheduler.mu.Unlock()
	s.telemetry.touch("loop", time.Now())
	ctx, cancel := context.WithTimeout(parent, budget)
	defer cancel()
	if s.alarmMaintenance(ctx) {
		return
	}
	var cursor struct {
		LastDeviceID       int64
		HistoryEnabled     bool
		HistoryParallelism int
	}
	r := s.db.WithContext(ctx).Raw(`SELECT last_device_id,history_enabled,history_parallelism FROM telemetry_scheduler WHERE id=1`).Scan(&cursor)
	if r.Error != nil || r.RowsAffected != 1 {
		s.telemetryLog("scheduler_read", time.Now())
		return
	}
	if !cursor.HistoryEnabled || cursor.HistoryParallelism < 1 || cursor.HistoryParallelism > 8 {
		return
	}
	// 时间窗每轮现算，随墙钟前进，过期消息自动出局：不需要人工设界，也没有
	// 「忘了回滚」的风险。同一轮内取任务与逐条复核共用这一个下界。
	floor := time.Now().UTC().Add(-inboxWindow).UnixMilli()
	s.inboxFloor.Store(floor)
	start := cursor.LastDeviceID
	items, err := s.inboxHeads(ctx, start, floor, 0, inboxPageSize)
	if err != nil {
		s.telemetryLog("inbox_read", time.Now())
		return
	}
	if len(items) < inboxPageSize && start > 0 {
		tail, e := s.inboxHeads(ctx, 0, floor, start, inboxPageSize-len(items))
		if e != nil {
			return
		}
		items = append(items, tail...)
		s.telemetry.touch("scheduler_wrap", time.Now())
	}
	jobs := make(chan model.Inbox, cursor.HistoryParallelism)
	var workers sync.WaitGroup
	for n := 0; n < cursor.HistoryParallelism; n++ {
		workers.Add(1)
		go func() {
			defer workers.Done()
			for item := range jobs {
				if ctx.Err() == nil {
					s.processInboxItem(ctx, item)
				}
			}
		}()
	}
	defer func() { close(jobs); workers.Wait() }()
	for begin := 0; begin < len(items); begin += cursor.HistoryParallelism {
		if ctx.Err() != nil {
			s.telemetry.touch("scheduler_budget", time.Now())
			return
		}
		group := items[begin:min(begin+cursor.HistoryParallelism, len(items))]
		if e := s.advanceTelemetryCursor(ctx, "last_device_id", group[len(group)-1].DeviceID); e != nil {
			return
		}
		for _, item := range group {
			s.telemetry.touch("scheduler_visit", time.Now())
			if item.NextAttemptAt != nil && item.NextAttemptAt.After(time.Now()) {
				continue
			}
			select {
			case jobs <- item:
			case <-ctx.Done():
				return
			}
		}
	}
}

func (s *Server) recordInboxFailure(ctx context.Context, item model.Inbox, code string, definite bool) {
	now := time.Now().UTC()
	var changed, quarantine bool
	err := service.TelemetryTransaction(s.db.WithContext(ctx), func(tx *gorm.DB) error {
		var current model.Inbox
		if err := tx.Raw(`SELECT * FROM telemetry_inbox WHERE id=? FOR UPDATE`, item.ID).Scan(&current).Error; err != nil {
			return err
		}
		if current.ID == 0 || current.ProcessedAt != nil {
			return nil
		}
		// Only observations made against this exact durable version may be counted.
		if current.Attempts != item.Attempts {
			return nil
		}
		// A separately committed exact positive TD confirmation supersedes an older
		// missing observation, without erasing the attempt/first-failure audit.
		if code == "td_missing" && current.TDConfirmedAt != nil {
			return nil
		}

		since := current.ReasonSince
		n := current.ReasonAttempts + 1
		if current.FailureCode == nil || *current.FailureCode != code || since == nil {
			since = &now
			n = 1
		}
		missingSince := current.MissingSince
		missingN := int64(0)
		var checked *time.Time
		if code == "td_missing" {
			checked = &now
			missingN = current.MissingAttempts + 1
			if missingSince == nil || current.MissingCheckedAt == nil || now.Sub(*current.MissingCheckedAt) > tdMissingMaxGap {
				missingSince = &now
				missingN = 1
			}
		} else {
			missingSince = nil
		}
		quarantine = definite || (code == "payload_invalid" && n >= 3 && now.Sub(*since) >= payloadGrace) ||
			(code == "td_missing" && missingN >= tdMissingMinChecks && now.Sub(*missingSince) >= tdMissingGrace)
		state := "retry"
		var finished, next *time.Time
		if quarantine {
			state = "quarantined"
			finished = &now
		} else {
			base := time.Second
			if code == "td_missing" {
				base = 5 * time.Second
			}
			at := now.Add(retryDelay(n, base, time.Minute))
			next = &at
		}
		r := tx.Model(&model.Inbox{}).Where("id=? AND processed_at IS NULL", item.ID).Updates(map[string]any{
			"business_state": state, "processed_at": finished, "first_failed_at": gorm.Expr("COALESCE(first_failed_at, ?)", now),
			"next_attempt_at": next, "attempts": gorm.Expr("attempts+1"), "failure_code": code,
			"reason_since": since, "reason_attempts": n, "missing_since": missingSince, "missing_attempts": missingN, "missing_checked_at": checked,
		})
		if r.Error != nil {
			return r.Error
		}
		changed = r.RowsAffected == 1
		return energy.MarkDirty(tx, item.DeviceID, item.TS)
	})
	s.telemetry.failure(code, quarantine && err == nil && changed, now)
	if err == nil && changed {
		if quarantine {
			s.telemetry.touch("quarantine", now)
		} else {
			s.telemetry.touch("retry", now)
		}
	}
	s.telemetryLog(code, now)
}

func (s *Server) advanceTelemetryCursor(ctx context.Context, column string, id int64) error {
	measured := time.Now()
	defer func() { s.telemetry.elapsed("cursor", time.Since(measured)) }()
	if column != "last_device_id" && column != "realtime_last_device_id" {
		return errors.New("invalid_cursor")
	}
	r := s.db.WithContext(ctx).Exec(`WITH gate AS MATERIALIZED(SELECT enabled FROM alarm_maintenance_state WHERE id=1 FOR SHARE)
 UPDATE telemetry_scheduler SET `+column+`=?,updated_at=clock_timestamp() FROM gate WHERE id=1 AND NOT gate.enabled`, id)
	if r.Error != nil {
		return r.Error
	}
	if r.RowsAffected != 1 {
		return service.ErrAlarmMaintenance
	}
	return nil
}
