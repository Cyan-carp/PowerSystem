//go:build ir_s02

package api

import (
	"context"
	"math"
	"testing"
	"time"

	"go.uber.org/zap"
	"gorm.io/gorm"
)

// 本文件是 2026-11-07「过期件自动收尾，看看效果」的定向复测。
// 复用 telemetry_expiry_pg_test.go 的一次性库守卫和夹具，不重跑 333 秒的
// 15 项全量矩阵；只覆盖本轮所有者关心的效果与安全边界：
//   - 8 天前 pending 在冻结周期内自动落终态，原行与历史字段保留；
//   - 窗口外 pending 与最老队龄下降，同设备窗口内 FIFO 不越队；
//   - 旧 owner_abandoned 行按关键字段摘要比较，零触碰；
//   - 默认关闭、维护暂停、history_enabled=false、重启重复运行安全。

type irS02OwnerFingerprint struct {
	N      int64  `gorm:"column:n"`
	MaxID  int64  `gorm:"column:max_id"`
	Digest string `gorm:"column:digest"`
}

func irS02OwnerFingerprintOf(t *testing.T, db *gorm.DB) irS02OwnerFingerprint {
	t.Helper()
	var fp irS02OwnerFingerprint
	err := db.Raw(`SELECT count(*) AS n,COALESCE(max(id),0) AS max_id,COALESCE(md5(string_agg(
 id::text||'|'||device_id::text||'|'||seq::text||'|'||ts_ms::text||'|'||
 COALESCE(business_state,'')||'|'||COALESCE(failure_code,'')||'|'||attempts::text||'|'||
 COALESCE(processed_at::text,'')||'|'||COALESCE(next_attempt_at::text,'')||'|'||payload::text,
 E'\n' ORDER BY id)),'') AS digest
 FROM telemetry_inbox WHERE failure_code='owner_abandoned'`).Scan(&fp).Error
	if err != nil {
		t.Fatal(err)
	}
	return fp
}

func irS02WindowExpiredCount(t *testing.T, db *gorm.DB) int64 {
	t.Helper()
	var n int64
	if err := db.Raw(`SELECT count(*) FROM telemetry_inbox WHERE business_state='quarantined' AND failure_code='window_expired'`).Scan(&n).Error; err != nil {
		t.Fatal(err)
	}
	return n
}

func irS02DeviceHead(t *testing.T, db *gorm.DB, deviceID int64) int64 {
	t.Helper()
	var id int64
	if err := db.Raw(`SELECT id FROM telemetry_inbox WHERE device_id=? AND processed_at IS NULL ORDER BY id ASC LIMIT 1`, deviceID).Scan(&id).Error; err != nil {
		t.Fatal(err)
	}
	return id
}

func TestIRS02ExpiryEffectShort(t *testing.T) {
	db, s := irS02Fixture(t)
	now := time.Now().UTC()
	oldTS := now.Add(-8 * 24 * time.Hour).UnixMilli()
	oldReceived := now.Add(-8 * 24 * time.Hour)
	firstFailed := oldReceived.Add(5 * time.Minute)
	reasonSince := firstFailed
	missingSince := firstFailed
	missingChecked := firstFailed.Add(time.Minute)
	tdConfirmed := firstFailed.Add(2 * time.Minute)
	oldProcessed := oldReceived.Add(time.Hour)

	// 设备 A：一条 8 天窗外行 + 两条窗口内行；过期后窗口内 FIFO 必须仍以
	// 30 秒那条为队头，5 分钟那条不能越队。
	a := irS02Device(t, db, "IR-S02-EFFECT-A")
	oldAID := irS02Insert(t, db, a, 1, oldTS, oldReceived, irS02Options{
		State: "pending", Attempts: 5, FailureCode: "td_missing",
		FirstFailedAt: &firstFailed, ReasonSince: &reasonSince, ReasonAttempts: 3,
		MissingSince: &missingSince, MissingCheckedAt: &missingChecked, MissingAttempts: 4,
		TDConfirmedAt: &tdConfirmed, AlarmEvaluation: "evaluated",
	})
	recentHeadID := irS02Insert(t, db, a, 2, now.Add(-30*time.Second).UnixMilli(), now.Add(-30*time.Second), irS02Options{})
	recentSecondID := irS02Insert(t, db, a, 3, now.Add(-5*time.Minute).UnixMilli(), now.Add(-5*time.Minute), irS02Options{})

	// 设备 B：另一条 8 天窗外行，验证单批可同时收尾多条且只收窗外。
	b := irS02Device(t, db, "IR-S02-EFFECT-B")
	oldBID := irS02Insert(t, db, b, 1, oldTS, oldReceived, irS02Options{Attempts: 2, FailureCode: "td_unavailable"})

	// 设备 C：只有窗口内 20 分钟行，作为队龄指标的对照组。
	c := irS02Device(t, db, "IR-S02-EFFECT-C")
	normalCID := irS02Insert(t, db, c, 1, now.Add(-20*time.Minute).UnixMilli(), now.Add(-20*time.Minute), irS02Options{})

	// 生产形态的 owner_abandoned：已处理终态、原因保留。摘要覆盖关键字段，
	// 任何过期路径误碰都会改变摘要。
	owner := irS02Device(t, db, "IR-S02-EFFECT-OWNER")
	ownerAID := irS02Insert(t, db, owner, 101, oldTS, oldReceived, irS02Options{
		State: "quarantined", FailureCode: "owner_abandoned", Attempts: 3, ProcessedAt: &oldProcessed,
	})
	ownerProcessed2 := oldProcessed.Add(time.Minute)
	ownerBID := irS02Insert(t, db, owner, 102, oldTS-1000, oldReceived, irS02Options{
		State: "quarantined", FailureCode: "owner_abandoned", ProcessedAt: &ownerProcessed2,
	})

	beforeOldA := irS02Load(t, db, oldAID)
	ownerBefore := irS02OwnerFingerprintOf(t, db)
	headBefore := irS02HeadAge(t, db, now)
	statsBefore := irS02QueueStats(t, s, now)
	if got := irS02PendingCount(t, db); got != 5 {
		t.Fatalf("pending before = %d, want 5", got)
	}
	if statsBefore["inbox_pending_rows_in_window"] != 3 || statsBefore["inbox_pending_rows_outside_window"] != 2 {
		t.Fatalf("scope before = %v", statsBefore)
	}
	if statsBefore["inbox_outside_window_oldest_age_seconds"] < 7*24*3600 {
		t.Fatalf("outside oldest age before = %v, want ~8d", statsBefore["inbox_outside_window_oldest_age_seconds"])
	}
	// 队头按设备、窗口内：A 贡献 30 秒，C 贡献 20 分钟；B 只有窗外行不计。
	if statsBefore["inbox_head_age_devices_total"] != 2 || statsBefore["inbox_head_age_devices_lt_1m"] != 1 ||
		statsBefore["inbox_head_age_devices_15m_1h"] != 1 || statsBefore["inbox_head_age_devices_ge_6h"] != 0 {
		t.Fatalf("head buckets before = %v", statsBefore)
	}

	// 真周期：不手动调用 expireInboxWindow，让冻结的 30 秒 worker 自己触发。
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		defer close(done)
		s.expiryWorker(ctx)
	}()
	start := time.Now()
	deadline := start.Add(50 * time.Second)
	for {
		if irS02Load(t, db, oldAID).ProcessedAt != nil {
			break
		}
		if time.Now().After(deadline) {
			cancel()
			<-done
			t.Fatalf("frozen cadence did not expire the 8-day row within 50s")
		}
		time.Sleep(250 * time.Millisecond)
	}
	elapsed := time.Since(start)
	cancel()
	<-done
	if elapsed < 20*time.Second {
		t.Fatalf("expired after %s, earlier than the frozen 30s cycle", elapsed)
	}
	if !irS02Terminal(irS02Load(t, db, oldAID)) || !irS02Terminal(irS02Load(t, db, oldBID)) {
		t.Fatal("both 8-day outside-window rows must reach quarantined/window_expired")
	}
	if got := irS02WindowExpiredCount(t, db); got != 2 {
		t.Fatalf("window_expired rows = %d, want 2", got)
	}

	afterOldA := irS02Load(t, db, oldAID)
	if afterOldA.NextAttemptAt != nil {
		t.Fatal("terminal row kept next_attempt_at")
	}
	if string(afterOldA.Payload) != string(beforeOldA.Payload) || afterOldA.Attempts != beforeOldA.Attempts ||
		!irS02TimeEqual(afterOldA.FirstFailedAt, beforeOldA.FirstFailedAt) ||
		!irS02TimeEqual(afterOldA.ReasonSince, beforeOldA.ReasonSince) || afterOldA.ReasonAttempts != beforeOldA.ReasonAttempts ||
		!irS02TimeEqual(afterOldA.MissingSince, beforeOldA.MissingSince) ||
		!irS02TimeEqual(afterOldA.MissingCheckedAt, beforeOldA.MissingCheckedAt) || afterOldA.MissingAttempts != beforeOldA.MissingAttempts ||
		!irS02TimeEqual(afterOldA.TDConfirmedAt, beforeOldA.TDConfirmedAt) ||
		afterOldA.AlarmEvaluation == nil || *afterOldA.AlarmEvaluation != "evaluated" ||
		!afterOldA.ReceivedAt.Equal(beforeOldA.ReceivedAt) {
		t.Fatalf("history fields changed: before=%+v after=%+v", beforeOldA, afterOldA)
	}
	var oldRowCount int64
	if err := db.Raw(`SELECT count(*) FROM telemetry_inbox WHERE id=?`, oldAID).Scan(&oldRowCount).Error; err != nil {
		t.Fatal(err)
	}
	if oldRowCount != 1 {
		t.Fatalf("original row count = %d, want 1 (row must be retained)", oldRowCount)
	}

	statsAfter := irS02QueueStats(t, s, time.Now().UTC())
	if statsAfter["inbox_pending_rows_all"] != 3 || statsAfter["inbox_pending_rows_in_window"] != 3 ||
		statsAfter["inbox_pending_rows_outside_window"] != 0 {
		t.Fatalf("scope after = %v", statsAfter)
	}
	if !math.IsNaN(statsAfter["inbox_outside_window_oldest_age_seconds"]) {
		t.Fatalf("outside oldest age after = %v, want NaN (no outside-window pending)", statsAfter["inbox_outside_window_oldest_age_seconds"])
	}
	if statsAfter["inbox_head_age_devices_total"] != 2 || statsAfter["inbox_head_age_devices_ge_6h"] != 0 ||
		statsAfter["inbox_head_age_devices_15m_1h"] != 1 ||
		statsAfter["inbox_head_age_devices_lt_1m"]+statsAfter["inbox_head_age_devices_1m_5m"] != 1 {
		t.Fatalf("head buckets after = %v", statsAfter)
	}
	if head := irS02DeviceHead(t, db, a.ID); head != recentHeadID {
		t.Fatalf("device A head = %d, want in-window FIFO head %d", head, recentHeadID)
	}
	for _, id := range []int64{recentHeadID, recentSecondID, normalCID} {
		if row := irS02Load(t, db, id); row.ProcessedAt != nil {
			t.Fatalf("in-window row %d was wrongly expired: %+v", id, row)
		}
	}
	if ownerAfter := irS02OwnerFingerprintOf(t, db); ownerAfter != ownerBefore {
		t.Fatalf("owner_abandoned fingerprint changed: before=%+v after=%+v", ownerBefore, ownerAfter)
	}
	var ownerRetained int64
	if err := db.Raw(`SELECT count(*) FROM telemetry_inbox WHERE id IN (?,?)`, ownerAID, ownerBID).Scan(&ownerRetained).Error; err != nil {
		t.Fatal(err)
	}
	if ownerRetained != 2 {
		t.Fatalf("terminal owner_abandoned rows retained = %d, want 2", ownerRetained)
	}
	headAfter := irS02HeadAge(t, db, time.Now().UTC())
	t.Logf("effect before: pending_all=5 in_window=3 outside=2 outside_oldest=%.0fs head_devices=2 legacy_head=%s",
		statsBefore["inbox_outside_window_oldest_age_seconds"], headBefore)
	t.Logf("effect after:  pending_all=3 in_window=3 outside=0 outside_oldest=NaN head_devices=2 legacy_head=%s expired=2 owner_rows=%d digest=%s",
		headAfter, ownerBefore.N, ownerBefore.Digest[:8])
	t.Logf("frozen cadence: 8-day rows became terminal after %s (interval=%s); in-window FIFO head stayed id=%d", elapsed, inboxExpiryInterval, recentHeadID)

	// 维护暂停：enabled=true 不写终态，恢复后继续。
	m := irS02Device(t, db, "IR-S02-EFFECT-MAINT")
	mID := irS02Insert(t, db, m, 1, oldTS, oldReceived, irS02Options{})
	if err := db.Exec("UPDATE alarm_maintenance_state SET enabled=true WHERE id=1").Error; err != nil {
		t.Fatal(err)
	}
	skipBefore := irS02MetricCount(s, "expiry_skip")
	s.expireInboxWindow(context.Background())
	if row := irS02Load(t, db, mID); row.ProcessedAt != nil {
		t.Fatal("maintenance enabled must stop expiry")
	}
	if irS02MetricCount(s, "expiry_skip") != skipBefore+1 {
		t.Fatalf("maintenance skip = %d, want %d", irS02MetricCount(s, "expiry_skip"), skipBefore+1)
	}
	if err := db.Exec("UPDATE alarm_maintenance_state SET enabled=false WHERE id=1").Error; err != nil {
		t.Fatal(err)
	}
	s.expireInboxWindow(context.Background())
	if !irS02Terminal(irS02Load(t, db, mID)) {
		t.Fatal("expiry did not resume after maintenance")
	}

	// history_enabled=false 是操作者暂停开关，必须 fail-closed。
	h := irS02Device(t, db, "IR-S02-EFFECT-HISTORY")
	hID := irS02Insert(t, db, h, 1, oldTS, oldReceived, irS02Options{})
	irS02SetHistoryEnabled(t, db, false)
	pausedBefore := irS02MetricCount(s, "expiry_paused")
	s.expireInboxWindow(context.Background())
	if row := irS02Load(t, db, hID); row.ProcessedAt != nil {
		t.Fatal("history_enabled=false must pause expiry writes")
	}
	if irS02MetricCount(s, "expiry_paused") != pausedBefore+1 {
		t.Fatalf("history paused = %d, want %d", irS02MetricCount(s, "expiry_paused"), pausedBefore+1)
	}
	irS02SetHistoryEnabled(t, db, true)
	s.expireInboxWindow(context.Background())
	if !irS02Terminal(irS02Load(t, db, hID)) {
		t.Fatal("expiry did not resume after history_enabled=true")
	}

	// 新 Server 实例重复运行：同一条终态不被二次改写。
	r := irS02Device(t, db, "IR-S02-EFFECT-RESTART")
	rID := irS02Insert(t, db, r, 1, oldTS, oldReceived, irS02Options{Attempts: 6})
	terminalBefore := irS02WindowExpiredCount(t, db)
	s2 := New(s.cfg, db, nil, nil, zap.NewNop())
	s2.expireInboxWindow(context.Background())
	if !irS02Terminal(irS02Load(t, db, rID)) {
		t.Fatal("new Server instance did not expire the outside-window row")
	}
	if got := irS02WindowExpiredCount(t, db); got != terminalBefore+1 {
		t.Fatalf("terminal count = %d, want %d", got, terminalBefore+1)
	}
	firstProcessed := irS02Load(t, db, rID).ProcessedAt
	s2.expireInboxWindow(context.Background())
	secondProcessed := irS02Load(t, db, rID).ProcessedAt
	if !irS02TimeEqual(firstProcessed, secondProcessed) {
		t.Fatalf("restart replay changed processed_at: %v -> %v", firstProcessed, secondProcessed)
	}
	if got := irS02WindowExpiredCount(t, db); got != terminalBefore+1 {
		t.Fatalf("restart replay changed terminal count: %d", got)
	}
	if got := irS02MetricCount(s2, "expiry_rows"); got != 1 {
		t.Fatalf("restart instance expiry_rows = %d, want 1 (second run 0)", got)
	}

	// 旧 owner_abandoned 的异常 pending/retry 行也不得被收尾；摘要与 count/max_id
	// 必须与过期前完全一致。
	pow := irS02Device(t, db, "IR-S02-EFFECT-OWNER-PENDING")
	pendingOwnerID := irS02Insert(t, db, pow, 1, oldTS, oldReceived, irS02Options{
		State: "pending", FailureCode: "owner_abandoned",
	})
	future := time.Now().UTC().Add(time.Hour)
	retryOwnerID := irS02Insert(t, db, pow, 2, oldTS, oldReceived, irS02Options{
		State: "retry", FailureCode: "owner_abandoned", NextAttemptAt: &future, Attempts: 1,
	})
	retryOwnerBefore := irS02Load(t, db, retryOwnerID)
	ownerPendingBefore := irS02OwnerFingerprintOf(t, db)
	s.expireInboxWindow(context.Background())
	if row := irS02Load(t, db, pendingOwnerID); row.ProcessedAt != nil || row.FailureCode == nil || *row.FailureCode != "owner_abandoned" {
		t.Fatalf("pending owner_abandoned row was touched: %+v", row)
	}
	if row := irS02Load(t, db, retryOwnerID); row.ProcessedAt != nil || row.FailureCode == nil ||
		*row.FailureCode != "owner_abandoned" || !irS02TimeEqual(row.NextAttemptAt, retryOwnerBefore.NextAttemptAt) {
		t.Fatalf("retry owner_abandoned row was touched: processed_at=%v failure=%v", row.ProcessedAt, row.FailureCode)
	}
	if ownerPendingAfter := irS02OwnerFingerprintOf(t, db); ownerPendingAfter != ownerPendingBefore {
		t.Fatalf("owner_abandoned pending fingerprint changed: before=%+v after=%+v", ownerPendingBefore, ownerPendingAfter)
	}
	t.Logf("owner_abandoned protected: rows=%d max_id=%d digest unchanged; pending/retry abnormal rows untouched", ownerPendingBefore.N, ownerPendingBefore.MaxID)

	// 默认关闭：New 后 expiry.Enabled=false；按 server.go 的启动门禁，worker
	// 不会被启动。等一个周期后 8 天行仍在 pending。
	off := irS02Device(t, db, "IR-S02-EFFECT-OFF")
	offID := irS02Insert(t, db, off, 1, oldTS, oldReceived, irS02Options{})
	cfgOff := s.cfg
	cfgOff.InboxExpiryEnabled = false
	sOff := New(cfgOff, db, nil, nil, zap.NewNop())
	if sOff.expiry.Enabled {
		t.Fatal("default-off server has expiry enabled")
	}
	ctxOff, cancelOff := context.WithCancel(context.Background())
	doneOff := make(chan struct{})
	launched := false
	go func() {
		defer close(doneOff)
		if sOff.expiry.Enabled {
			launched = true
			sOff.expiryWorker(ctxOff)
		}
	}()
	time.Sleep(31 * time.Second)
	cancelOff()
	<-doneOff
	if launched {
		t.Fatal("default-off server launched the expiry worker")
	}
	if row := irS02Load(t, db, offID); row.ProcessedAt != nil {
		t.Fatal("default-off server expired an 8-day pending row")
	}
	t.Log("default off: expiry.Enabled=false, worker not launched, 8-day row still pending after one 30s cycle")
}
