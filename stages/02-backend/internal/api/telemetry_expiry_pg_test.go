//go:build ir_s02

package api

import (
	"context"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"go.uber.org/zap"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"powersystem/backend/internal/config"
	"powersystem/backend/internal/model"
	"powersystem/backend/internal/store"
)

// 本文件只跑在一次性隔离库 ir_s02_test。没有专用 DSN 时必须失败而不是跳过，
// 避免把“没连库”误报成通过；也不会触碰 home-dev 业务库或生产。
func irS02Fixture(t *testing.T) (*gorm.DB, *Server) {
	t.Helper()
	dsn := os.Getenv("IR_S02_TEST_DSN")
	if os.Getenv("IR_S02_ISOLATED") != "1" || dsn == "" {
		t.Fatal("isolated IR-S02 PostgreSQL required: set IR_S02_ISOLATED=1 and IR_S02_TEST_DSN")
	}
	db, err := gorm.Open(postgres.Open(dsn), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		t.Fatal(err)
	}
	pool, err := db.DB()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { pool.Close() })
	var env struct {
		DB      string
		Address string
		Port    int
	}
	if err := db.Raw(`SELECT current_database() AS db,host(inet_server_addr()) AS address,inet_server_port() AS port`).Scan(&env).Error; err != nil {
		t.Fatal(err)
	}
	ip := net.ParseIP(env.Address)
	if env.DB != "ir_s02_test" || env.Port != 5432 || ip == nil || !(ip.IsLoopback() || ip.IsPrivate()) {
		t.Fatalf("refusing non-isolated database: %+v", env)
	}
	if err := db.Exec("DROP SCHEMA public CASCADE; CREATE SCHEMA public").Error; err != nil {
		t.Fatal(err)
	}
	for _, path := range store.MigrationPaths {
		if err := store.Migrate(db, filepath.Join("../..", path)); err != nil {
			t.Fatalf("migrate %s: %v", path, err)
		}
	}
	if err := db.Exec("UPDATE telemetry_scheduler SET history_enabled=true,history_parallelism=8 WHERE id=1").Error; err != nil {
		t.Fatal(err)
	}
	cfg := config.Config{
		SimulationAlarmMode:        "off",
		JWTSecret:                  strings.Repeat("x", 32),
		InboxExpiryEnabled:         true,
		InboxExpiryIntervalSeconds: 30,
		InboxExpiryBatch:           2000,
		InboxExpiryBudgetSeconds:   5,
	}
	s := New(cfg, db, nil, nil, zap.NewNop())
	return db, s
}

type irS02Options struct {
	State            string
	FailureCode      string
	Attempts         int64
	FirstFailedAt    *time.Time
	NextAttemptAt    *time.Time
	ReasonSince      *time.Time
	ReasonAttempts   int64
	MissingSince     *time.Time
	MissingCheckedAt *time.Time
	MissingAttempts  int64
	TDConfirmedAt    *time.Time
	AlarmEvaluation  string
	ProcessedAt      *time.Time
}

func irS02Ptr[T any](v T) *T { return &v }

func irS02Device(t *testing.T, db *gorm.DB, code string) model.Device {
	t.Helper()
	d := model.Device{DeviceCode: code, Name: code, DevType: "inverter", StationCode: "IR-S02", Vendor: "fixture"}
	if err := db.Create(&d).Error; err != nil {
		t.Fatal(err)
	}
	return d
}

func irS02Insert(t *testing.T, db *gorm.DB, d model.Device, seq, ts int64, received time.Time, opts irS02Options) int64 {
	t.Helper()
	if opts.State == "" {
		opts.State = "pending"
	}
	payload := fmt.Sprintf(`{"schema_version":1,"run_id":"ir-s02","device_code":%q,"station_code":%q,"seq":%d,"ts_ms":%d,"voltage":400,"current":100,"power":68.5,"temperature":40,"status":1}`, d.DeviceCode, d.StationCode, seq, ts)
	var failureCode, alarmEvaluation any
	if opts.FailureCode != "" {
		failureCode = opts.FailureCode
	}
	if opts.AlarmEvaluation != "" {
		alarmEvaluation = opts.AlarmEvaluation
	}
	var id int64
	err := db.Raw(`INSERT INTO telemetry_inbox(run_id,device_id,seq,ts_ms,payload,received_at,business_state,attempts,failure_code,first_failed_at,next_attempt_at,reason_since,reason_attempts,missing_since,missing_checked_at,missing_attempts,td_confirmed_at,alarm_evaluation,processed_at)
VALUES(?,?,?,?,?::jsonb,?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id`,
		"ir-s02", d.ID, seq, ts, payload, received, opts.State, opts.Attempts, failureCode, opts.FirstFailedAt, opts.NextAttemptAt, opts.ReasonSince, opts.ReasonAttempts, opts.MissingSince, opts.MissingCheckedAt, opts.MissingAttempts, opts.TDConfirmedAt, alarmEvaluation, opts.ProcessedAt).Scan(&id).Error
	if err != nil {
		t.Fatalf("insert inbox: %v", err)
	}
	return id
}

func irS02BulkOld(t *testing.T, db *gorm.DB, d model.Device, runID string, seqBase, n int, ts int64, received time.Time) {
	t.Helper()
	err := db.Exec(`INSERT INTO telemetry_inbox(run_id,device_id,seq,ts_ms,payload,received_at,business_state)
SELECT ?,?,g,?, '{}'::jsonb,?,'pending' FROM generate_series(?::bigint,?::bigint) g`,
		runID, d.ID, ts, received, seqBase, seqBase+n-1).Error
	if err != nil {
		t.Fatal(err)
	}
}

func irS02Load(t *testing.T, db *gorm.DB, id int64) model.Inbox {
	t.Helper()
	var row model.Inbox
	if err := db.Raw("SELECT * FROM telemetry_inbox WHERE id=?", id).Scan(&row).Error; err != nil {
		t.Fatal(err)
	}
	return row
}

func irS02Terminal(row model.Inbox) bool {
	return row.ProcessedAt != nil && row.BusinessState != nil && *row.BusinessState == "quarantined" &&
		row.FailureCode != nil && *row.FailureCode == "window_expired"
}

func irS02TimeEqual(a, b *time.Time) bool {
	if a == nil || b == nil {
		return a == nil && b == nil
	}
	return a.Equal(*b)
}

func irS02PendingCount(t *testing.T, db *gorm.DB) int64 {
	t.Helper()
	var n int64
	if err := db.Raw("SELECT count(*) FROM telemetry_inbox WHERE processed_at IS NULL").Scan(&n).Error; err != nil {
		t.Fatal(err)
	}
	return n
}

func irS02HeadAge(t *testing.T, db *gorm.DB, now time.Time) time.Duration {
	t.Helper()
	var at *time.Time
	if err := db.Raw("SELECT received_at FROM telemetry_inbox WHERE processed_at IS NULL ORDER BY id LIMIT 1").Scan(&at).Error; err != nil {
		t.Fatal(err)
	}
	if at == nil {
		return 0
	}
	return now.Sub(*at)
}

func irS02MetricCount(s *Server, key string) uint64 {
	s.telemetry.mu.Lock()
	defer s.telemetry.mu.Unlock()
	return s.telemetry.counts[key]
}

func irS02QueueStats(t *testing.T, s *Server, now time.Time) map[string]float64 {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	v, err := s.inboxQueueStats(ctx, now, inboxHeadAgeDeviceLimit)
	if err != nil {
		t.Fatal(err)
	}
	return v
}

func irS02SetHistoryEnabled(t *testing.T, db *gorm.DB, enabled bool) {
	t.Helper()
	if err := db.Exec("UPDATE telemetry_scheduler SET history_enabled=? WHERE id=1", enabled).Error; err != nil {
		t.Fatal(err)
	}
}

func irS02ResetScheduler(t *testing.T, db *gorm.DB) {
	t.Helper()
	if err := db.Exec("DELETE FROM telemetry_scheduler WHERE id=1").Error; err != nil {
		t.Fatal(err)
	}
	if err := db.Exec("INSERT INTO telemetry_scheduler(id,history_enabled,history_parallelism) VALUES(1,true,8)").Error; err != nil {
		t.Fatal(err)
	}
}

func irS02Plan(t *testing.T, db *gorm.DB, query string, args ...any) ([]string, time.Duration) {
	t.Helper()
	tx := db.Begin()
	if tx.Error != nil {
		t.Fatal(tx.Error)
	}
	defer tx.Rollback()
	start := time.Now()
	var plan []string
	if err := tx.Raw("EXPLAIN (ANALYZE, BUFFERS, COSTS OFF) "+query, args...).Scan(&plan).Error; err != nil {
		t.Fatal(err)
	}
	return plan, time.Since(start)
}

func TestIRS02ExpiryIsolated(t *testing.T) {
	t.Run("T01_expired_row_terminal_and_head_age_drops", func(t *testing.T) {
		db, s := irS02Fixture(t)
		d := irS02Device(t, db, "IR-S02-T01")
		now := time.Now().UTC()
		oldTS := now.Add(-8 * 24 * time.Hour).UnixMilli()
		oldReceived := now.Add(-8 * 24 * time.Hour)
		firstFailed := oldReceived.Add(5 * time.Minute)
		reasonSince := firstFailed
		missingSince := firstFailed
		missingChecked := firstFailed.Add(time.Minute)
		tdConfirmed := firstFailed.Add(2 * time.Minute)
		oldID := irS02Insert(t, db, d, 1, oldTS, oldReceived, irS02Options{
			State: "pending", Attempts: 7, FailureCode: "td_missing",
			FirstFailedAt: &firstFailed, ReasonSince: &reasonSince, ReasonAttempts: 4,
			MissingSince: &missingSince, MissingCheckedAt: &missingChecked, MissingAttempts: 6,
			TDConfirmedAt: &tdConfirmed, AlarmEvaluation: "evaluated",
		})
		recentID := irS02Insert(t, db, d, 2, now.Add(-30*time.Second).UnixMilli(), now.Add(-30*time.Second), irS02Options{State: "pending"})
		before := irS02Load(t, db, oldID)
		pendingBefore := irS02PendingCount(t, db)
		statsBefore := irS02QueueStats(t, s, now)
		headBefore := irS02HeadAge(t, db, now)
		if pendingBefore != 2 {
			t.Fatalf("pending before = %d", pendingBefore)
		}
		if statsBefore["inbox_pending_rows_outside_window"] != 1 || statsBefore["inbox_pending_rows_in_window"] != 1 {
			t.Fatalf("scope counts before = %v", statsBefore)
		}
		// S01 口径：窗口外 8 天行不参与队头；该设备窗口内最旧的一行是 30 秒前。
		if statsBefore["inbox_head_age_devices_total"] != 1 || statsBefore["inbox_head_age_devices_lt_1m"] != 1 ||
			statsBefore["inbox_head_age_devices_ge_6h"] != 0 {
			t.Fatalf("head buckets before = %v", statsBefore)
		}
		if headBefore < 7*24*time.Hour {
			t.Fatalf("legacy head age before = %s, expected the 8-day row", headBefore)
		}

		s.expireInboxWindow(context.Background())

		after := irS02Load(t, db, oldID)
		if !irS02Terminal(after) {
			t.Fatalf("expired row not terminal: %+v", after)
		}
		if after.NextAttemptAt != nil {
			t.Fatal("terminal row kept next_attempt_at")
		}
		if string(after.Payload) != string(before.Payload) || after.Attempts != before.Attempts ||
			!irS02TimeEqual(after.FirstFailedAt, before.FirstFailedAt) ||
			!irS02TimeEqual(after.ReasonSince, before.ReasonSince) || after.ReasonAttempts != before.ReasonAttempts ||
			!irS02TimeEqual(after.MissingSince, before.MissingSince) ||
			!irS02TimeEqual(after.MissingCheckedAt, before.MissingCheckedAt) || after.MissingAttempts != before.MissingAttempts ||
			!irS02TimeEqual(after.TDConfirmedAt, before.TDConfirmedAt) ||
			after.AlarmEvaluation == nil || *after.AlarmEvaluation != "evaluated" {
			t.Fatalf("history fields changed: before=%+v after=%+v", before, after)
		}
		if !after.ReceivedAt.Equal(before.ReceivedAt) {
			t.Fatalf("received_at changed: %s -> %s", before.ReceivedAt, after.ReceivedAt)
		}
		recent := irS02Load(t, db, recentID)
		if recent.ProcessedAt != nil {
			t.Fatal("recent row must stay pending")
		}
		if got := irS02PendingCount(t, db); got != pendingBefore-1 {
			t.Fatalf("logical pending count = %d, want %d", got, pendingBefore-1)
		}
		statsAfter := irS02QueueStats(t, s, now.Add(time.Second))
		if statsAfter["inbox_pending_rows_outside_window"] != 0 || statsAfter["inbox_pending_rows_all"] != 1 {
			t.Fatalf("scope counts after = %v", statsAfter)
		}
		if statsAfter["inbox_head_age_devices_total"] != 1 || statsAfter["inbox_head_age_devices_lt_1m"] != 1 ||
			statsAfter["inbox_head_age_devices_ge_6h"] != 0 {
			t.Fatalf("head buckets after = %v", statsAfter)
		}
		if headAfter := irS02HeadAge(t, db, now.Add(time.Second)); headAfter > 2*time.Minute {
			t.Fatalf("head age after = %s; expired row still dominates the queue", headAfter)
		}
		if irS02MetricCount(s, "expiry_rows") != 1 {
			t.Fatalf("expiry_rows = %d", irS02MetricCount(s, "expiry_rows"))
		}
		t.Logf("before legacy_head=%s outside=1; after legacy_head=%s outside=0; per-device head stays <1m", headBefore, irS02HeadAge(t, db, now.Add(time.Second)))
	})

	t.Run("T02_seven_day_boundary_and_recent_rows", func(t *testing.T) {
		db, s := irS02Fixture(t)
		d := irS02Device(t, db, "IR-S02-T02")
		now := time.Now().UTC()
		floor := now.Add(-inboxWindow).UnixMilli()
		expiredID := irS02Insert(t, db, d, 1, floor-1, now, irS02Options{})
		exactID := irS02Insert(t, db, d, 2, floor, now, irS02Options{})
		insideID := irS02Insert(t, db, d, 3, floor+1, now, irS02Options{})
		recentID := irS02Insert(t, db, d, 4, now.UnixMilli(), now, irS02Options{})

		changed, err := s.expireInboxBatch(context.Background(), floor, 500)
		if err != nil {
			t.Fatal(err)
		}
		if changed != 1 {
			t.Fatalf("changed = %d, want exactly the floor-1 row", changed)
		}
		if !irS02Terminal(irS02Load(t, db, expiredID)) {
			t.Fatal("floor-1 row not terminal")
		}
		for name, id := range map[string]int64{"exact floor": exactID, "floor+1": insideID, "recent": recentID} {
			if row := irS02Load(t, db, id); row.ProcessedAt != nil {
				t.Fatalf("%s row must remain pending: %+v", name, row)
			}
		}
		stats := irS02QueueStats(t, s, now)
		if stats["inbox_pending_rows_outside_window"] != 0 || stats["inbox_pending_rows_in_window"] != 3 {
			t.Fatalf("boundary scope = %v", stats)
		}
		// 恰好等于 floor 的行仍在窗口内，且是该设备最旧窗口内行；年龄按
		// received_at 计算，所以是刚接收的 <1m，而不是 ts 的 7 天。
		if stats["inbox_head_age_devices_total"] != 1 || stats["inbox_head_age_devices_lt_1m"] != 1 {
			t.Fatalf("boundary head = %v", stats)
		}
		t.Log("only ts_ms < floor expired; exact floor and newer rows untouched; exact floor is the per-device in-window head")
	})

	t.Run("T03_retry_expires_owner_abandoned_protected", func(t *testing.T) {
		db, s := irS02Fixture(t)
		d := irS02Device(t, db, "IR-S02-T03")
		now := time.Now().UTC()
		oldTS := now.Add(-9 * 24 * time.Hour).UnixMilli()
		future := now.Add(time.Hour)
		retryID := irS02Insert(t, db, d, 1, oldTS, now.Add(-9*24*time.Hour), irS02Options{
			State: "retry", Attempts: 2, FailureCode: "td_unavailable", NextAttemptAt: &future,
		})
		pendingOwnerID := irS02Insert(t, db, d, 2, oldTS, now.Add(-9*24*time.Hour), irS02Options{
			State: "pending", FailureCode: "owner_abandoned",
		})
		terminalOwnerID := irS02Insert(t, db, d, 3, oldTS, now.Add(-9*24*time.Hour), irS02Options{
			State: "quarantined", FailureCode: "owner_abandoned", ProcessedAt: &now,
		})
		var ownerBefore struct {
			N     int64
			MaxID int64
		}
		if err := db.Raw(`SELECT count(*) AS n,COALESCE(max(id),0) AS max_id FROM telemetry_inbox WHERE failure_code='owner_abandoned'`).Scan(&ownerBefore).Error; err != nil {
			t.Fatal(err)
		}

		s.expireInboxWindow(context.Background())

		if !irS02Terminal(irS02Load(t, db, retryID)) {
			t.Fatal("retry row outside the window must reach terminal")
		}
		if row := irS02Load(t, db, pendingOwnerID); row.ProcessedAt != nil || row.FailureCode == nil || *row.FailureCode != "owner_abandoned" {
			t.Fatalf("pending owner_abandoned row was touched: %+v", row)
		}
		if row := irS02Load(t, db, terminalOwnerID); row.FailureCode == nil || *row.FailureCode != "owner_abandoned" || row.ProcessedAt == nil {
			t.Fatalf("terminal owner_abandoned row changed: %+v", row)
		}
		var ownerAfter struct {
			N     int64
			MaxID int64
		}
		if err := db.Raw(`SELECT count(*) AS n,COALESCE(max(id),0) AS max_id FROM telemetry_inbox WHERE failure_code='owner_abandoned'`).Scan(&ownerAfter).Error; err != nil {
			t.Fatal(err)
		}
		if ownerAfter != ownerBefore {
			t.Fatalf("owner_abandoned range changed: before=%+v after=%+v", ownerBefore, ownerAfter)
		}
		stats := irS02QueueStats(t, s, now)
		if stats["inbox_head_age_devices_total"] != 0 || stats["inbox_pending_rows_outside_window"] != 1 {
			t.Fatalf("owner-only queue stats = %v", stats)
		}
		t.Logf("owner_abandoned rows=%d max_id=%d unchanged; retry row expired by window", ownerAfter.N, ownerAfter.MaxID)
	})

	t.Run("T04_maintenance_pause_and_missing_gate", func(t *testing.T) {
		db, s := irS02Fixture(t)
		d := irS02Device(t, db, "IR-S02-T04")
		now := time.Now().UTC()
		oldTS := now.Add(-8 * 24 * time.Hour).UnixMilli()
		firstID := irS02Insert(t, db, d, 1, oldTS, now.Add(-8*24*time.Hour), irS02Options{})
		if err := db.Exec("UPDATE alarm_maintenance_state SET enabled=true WHERE id=1").Error; err != nil {
			t.Fatal(err)
		}
		s.expireInboxWindow(context.Background())
		if row := irS02Load(t, db, firstID); row.ProcessedAt != nil {
			t.Fatal("maintenance enabled must stop the expiry write")
		}
		if irS02MetricCount(s, "expiry_skip") != 1 || irS02MetricCount(s, "expiry_rows") != 0 {
			t.Fatalf("maintenance metrics: skip=%d rows=%d", irS02MetricCount(s, "expiry_skip"), irS02MetricCount(s, "expiry_rows"))
		}
		if err := db.Exec("UPDATE alarm_maintenance_state SET enabled=false WHERE id=1").Error; err != nil {
			t.Fatal(err)
		}
		s.expireInboxWindow(context.Background())
		if !irS02Terminal(irS02Load(t, db, firstID)) {
			t.Fatal("expiry did not resume after maintenance")
		}

		secondID := irS02Insert(t, db, d, 2, oldTS, now.Add(-8*24*time.Hour), irS02Options{})
		if err := db.Exec("DELETE FROM alarm_maintenance_state WHERE id=1").Error; err != nil {
			t.Fatal(err)
		}
		s.expireInboxWindow(context.Background())
		if row := irS02Load(t, db, secondID); row.ProcessedAt != nil {
			t.Fatal("missing maintenance gate must fail closed")
		}
		if irS02MetricCount(s, "expiry_skip") != 2 {
			t.Fatalf("missing gate skip = %d", irS02MetricCount(s, "expiry_skip"))
		}
		if err := db.Exec("INSERT INTO alarm_maintenance_state(id,enabled) VALUES(1,false)").Error; err != nil {
			t.Fatal(err)
		}
		s.expireInboxWindow(context.Background())
		if !irS02Terminal(irS02Load(t, db, secondID)) {
			t.Fatal("expiry did not recover after gate restored")
		}
	})

	t.Run("T05_concurrent_and_repeat_runs_are_idempotent", func(t *testing.T) {
		db, s := irS02Fixture(t)
		d := irS02Device(t, db, "IR-S02-T05")
		now := time.Now().UTC()
		oldTS := now.Add(-8 * 24 * time.Hour).UnixMilli()
		ids := make([]int64, 0, 120)
		for i := int64(1); i <= 120; i++ {
			ids = append(ids, irS02Insert(t, db, d, i, oldTS, now.Add(-8*24*time.Hour), irS02Options{}))
		}
		floor := now.Add(-inboxWindow).UnixMilli()
		var wg sync.WaitGroup
		results := make(chan int64, 2)
		errs := make(chan error, 2)
		for i := 0; i < 2; i++ {
			wg.Add(1)
			go func() {
				defer wg.Done()
				n, err := s.expireInboxBatch(context.Background(), floor, 200)
				results <- n
				errs <- err
			}()
		}
		wg.Wait()
		close(results)
		close(errs)
		var sum int64
		for n := range results {
			sum += n
		}
		for err := range errs {
			if err != nil {
				t.Fatal(err)
			}
		}
		if sum != 120 {
			t.Fatalf("concurrent sum = %d, want 120 (no double processing)", sum)
		}
		for _, id := range ids {
			if !irS02Terminal(irS02Load(t, db, id)) {
				t.Fatalf("row %d not terminal", id)
			}
		}
		again, err := s.expireInboxBatch(context.Background(), floor, 200)
		if err != nil || again != 0 {
			t.Fatalf("repeat run changed=%d err=%v, want 0/nil", again, err)
		}
		if got := irS02PendingCount(t, db); got != 0 {
			t.Fatalf("pending after = %d", got)
		}
		t.Logf("two concurrent batches changed %d rows total; repeat=0; logical pending=0", sum)
	})

	t.Run("T06_timeout_rolls_back_then_recovers", func(t *testing.T) {
		db, s := irS02Fixture(t)
		d := irS02Device(t, db, "IR-S02-T06")
		now := time.Now().UTC()
		oldTS := now.Add(-8 * 24 * time.Hour).UnixMilli()
		id := irS02Insert(t, db, d, 1, oldTS, now.Add(-8*24*time.Hour), irS02Options{State: "pending", Attempts: 3, FailureCode: "td_unavailable"})
		if err := db.Exec(`CREATE OR REPLACE FUNCTION ir_s02_slow_update() RETURNS trigger AS $$
BEGIN
 PERFORM pg_sleep(1);
 RETURN NEW;
END
$$ LANGUAGE plpgsql`).Error; err != nil {
			t.Fatal(err)
		}
		if err := db.Exec("DROP TRIGGER IF EXISTS ir_s02_slow_update ON telemetry_inbox").Error; err != nil {
			t.Fatal(err)
		}
		if err := db.Exec("CREATE TRIGGER ir_s02_slow_update BEFORE UPDATE ON telemetry_inbox FOR EACH ROW EXECUTE FUNCTION ir_s02_slow_update()").Error; err != nil {
			t.Fatal(err)
		}
		ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
		_, err := s.expireInboxBatch(ctx, now.Add(-inboxWindow).UnixMilli(), 500)
		cancel()
		if err == nil {
			t.Fatal("slow expiry should hit the transaction budget")
		}
		if row := irS02Load(t, db, id); row.ProcessedAt != nil {
			t.Fatalf("timed-out expiry left a partial terminal row: %+v", row)
		}
		if err := db.Exec("DROP TRIGGER IF EXISTS ir_s02_slow_update ON telemetry_inbox").Error; err != nil {
			t.Fatal(err)
		}
		if err := db.Exec("DROP FUNCTION IF EXISTS ir_s02_slow_update()").Error; err != nil {
			t.Fatal(err)
		}
		s.expireInboxWindow(context.Background())
		if !irS02Terminal(irS02Load(t, db, id)) {
			t.Fatal("expiry did not recover after timeout")
		}
		t.Logf("timeout error=%v; row stayed pending, then normal run reached terminal", err)
	})

	t.Run("T07_restart_replay_is_idempotent", func(t *testing.T) {
		db, s := irS02Fixture(t)
		d := irS02Device(t, db, "IR-S02-T07")
		now := time.Now().UTC()
		id := irS02Insert(t, db, d, 1, now.Add(-8*24*time.Hour).UnixMilli(), now.Add(-8*24*time.Hour), irS02Options{Attempts: 5})
		s.expireInboxWindow(context.Background())
		first := irS02Load(t, db, id)
		if !irS02Terminal(first) {
			t.Fatal("first run not terminal")
		}
		s2 := New(s.cfg, db, nil, nil, zap.NewNop())
		s2.expireInboxWindow(context.Background())
		second := irS02Load(t, db, id)
		if !irS02TimeEqual(first.ProcessedAt, second.ProcessedAt) || second.Attempts != first.Attempts || string(second.Payload) != string(first.Payload) {
			t.Fatalf("restart replay changed terminal row: first=%+v second=%+v", first, second)
		}
		if irS02MetricCount(s2, "expiry_rows") != 0 {
			t.Fatalf("restart replay rows = %d", irS02MetricCount(s2, "expiry_rows"))
		}
		t.Log("new Server instance re-ran expiry and changed 0 rows")
	})

	t.Run("T08_frozen_cadence", func(t *testing.T) {
		db, s := irS02Fixture(t)
		d := irS02Device(t, db, "IR-S02-T08")
		now := time.Now().UTC()
		id := irS02Insert(t, db, d, 1, now.Add(-8*24*time.Hour).UnixMilli(), now.Add(-8*24*time.Hour), irS02Options{})
		ctx, cancel := context.WithCancel(context.Background())
		done := make(chan struct{})
		go func() {
			defer close(done)
			s.expiryWorker(ctx)
		}()
		defer func() {
			cancel()
			<-done
		}()
		start := time.Now()
		deadline := start.Add(50 * time.Second)
		for {
			if irS02Load(t, db, id).ProcessedAt != nil {
				break
			}
			if time.Now().After(deadline) {
				t.Fatalf("frozen cadence did not expire the row within 50s")
			}
			time.Sleep(250 * time.Millisecond)
		}
		elapsed := time.Since(start)
		if elapsed < 20*time.Second {
			t.Fatalf("row expired after %s, earlier than the frozen 30s cycle", elapsed)
		}
		if !irS02Terminal(irS02Load(t, db, id)) {
			t.Fatal("cadence run did not reach window_expired terminal")
		}
		if irS02MetricCount(s, "expiry_rows") < 1 {
			t.Fatalf("expiry_rows = %d", irS02MetricCount(s, "expiry_rows"))
		}
		t.Logf("frozen cadence: 8-day pending row became terminal after %s (interval=%s)", elapsed, inboxExpiryInterval)
	})

	t.Run("T11_frozen_batch_fits_budget", func(t *testing.T) {
		db, s := irS02Fixture(t)
		d := irS02Device(t, db, "IR-S02-T11")
		now := time.Now().UTC()
		oldTS := now.Add(-8 * 24 * time.Hour).UnixMilli()
		irS02BulkOld(t, db, d, "ir-s02-batch", 1, inboxExpiryBatch, oldTS, now.Add(-8*24*time.Hour))
		start := time.Now()
		changed, err := s.expireInboxBatch(context.Background(), now.Add(-inboxWindow).UnixMilli(), inboxExpiryBatch)
		elapsed := time.Since(start)
		if err != nil {
			t.Fatal(err)
		}
		if changed != int64(inboxExpiryBatch) {
			t.Fatalf("changed = %d, want %d", changed, inboxExpiryBatch)
		}
		if elapsed >= inboxExpiryBudget {
			t.Fatalf("frozen batch took %s, exceeds %s budget", elapsed, inboxExpiryBudget)
		}
		if got := irS02PendingCount(t, db); got != 0 {
			t.Fatalf("pending after batch = %d", got)
		}
		rate := float64(changed) / elapsed.Seconds() * 3600
		t.Logf("frozen batch: %d rows in %s (< %s budget; interval=%s); single-batch isolated rate=%.0f rows/h", changed, elapsed, inboxExpiryBudget, inboxExpiryInterval, rate)
	})

	t.Run("T12_per_device_head_is_one_in_window_row", func(t *testing.T) {
		db, s := irS02Fixture(t)
		now := time.Now().UTC()
		oldTS := now.Add(-8 * 24 * time.Hour).UnixMilli()
		a := irS02Device(t, db, "IR-S02-T12-A")
		b := irS02Device(t, db, "IR-S02-T12-B")
		c := irS02Device(t, db, "IR-S02-T12-C")
		d := irS02Device(t, db, "IR-S02-T12-D")
		// A：窗外行 + 两条窗口内行；只应贡献最旧窗口内的一条（30 秒）。
		irS02Insert(t, db, a, 1, oldTS, now.Add(-8*24*time.Hour), irS02Options{})
		irS02Insert(t, db, a, 2, now.Add(-30*time.Second).UnixMilli(), now.Add(-30*time.Second), irS02Options{})
		irS02Insert(t, db, a, 3, now.Add(-10*time.Minute).UnixMilli(), now.Add(-10*time.Minute), irS02Options{})
		// B：窗口内 20 分钟。
		irS02Insert(t, db, b, 1, now.Add(-20*time.Minute).UnixMilli(), now.Add(-20*time.Minute), irS02Options{})
		// C：只有窗外行，不贡献队头。
		irS02Insert(t, db, c, 1, oldTS, now.Add(-8*24*time.Hour), irS02Options{})
		// D：窗口内 2 小时 + 1 分钟；只应贡献 2 小时。
		irS02Insert(t, db, d, 1, now.Add(-2*time.Hour).UnixMilli(), now.Add(-2*time.Hour), irS02Options{})
		irS02Insert(t, db, d, 2, now.Add(-time.Minute).UnixMilli(), now.Add(-time.Minute), irS02Options{})

		stats := irS02QueueStats(t, s, now)
		if stats["inbox_pending_rows_outside_window"] != 2 || stats["inbox_pending_rows_in_window"] != 5 {
			t.Fatalf("scope counts = %v", stats)
		}
		if stats["inbox_head_age_devices_total"] != 3 {
			t.Fatalf("devices total = %v, want A/B/D only (C has no in-window row)", stats["inbox_head_age_devices_total"])
		}
		if stats["inbox_head_age_devices_lt_1m"] != 1 || stats["inbox_head_age_devices_15m_1h"] != 1 ||
			stats["inbox_head_age_devices_1h_6h"] != 1 || stats["inbox_head_age_devices_ge_6h"] != 0 {
			t.Fatalf("per-device buckets = %v", stats)
		}
		if stats["inbox_head_age_devices_1m_5m"] != 0 || stats["inbox_head_age_devices_5m_15m"] != 0 {
			t.Fatalf("duplicate or wrong bucket = %v", stats)
		}
		t.Log("A contributes only its 30s head; B 20m; D 2h; C outside-window-only contributes nothing; no duplicates")
	})

	t.Run("T13_history_enabled_pauses_expiry", func(t *testing.T) {
		db, s := irS02Fixture(t)
		d := irS02Device(t, db, "IR-S02-T13")
		now := time.Now().UTC()
		oldTS := now.Add(-8 * 24 * time.Hour).UnixMilli()
		id := irS02Insert(t, db, d, 1, oldTS, now.Add(-8*24*time.Hour), irS02Options{})

		irS02SetHistoryEnabled(t, db, false)
		s.expireInboxWindow(context.Background())
		if row := irS02Load(t, db, id); row.ProcessedAt != nil {
			t.Fatal("history_enabled=false must pause expiry writes")
		}
		if irS02MetricCount(s, "expiry_paused") != 1 || irS02MetricCount(s, "expiry_rows") != 0 {
			t.Fatalf("paused metrics: paused=%d rows=%d", irS02MetricCount(s, "expiry_paused"), irS02MetricCount(s, "expiry_rows"))
		}

		irS02SetHistoryEnabled(t, db, true)
		s.expireInboxWindow(context.Background())
		if !irS02Terminal(irS02Load(t, db, id)) {
			t.Fatal("expiry did not resume after history_enabled=true")
		}

		secondID := irS02Insert(t, db, d, 2, oldTS, now.Add(-8*24*time.Hour), irS02Options{})
		if err := db.Exec("DELETE FROM telemetry_scheduler WHERE id=1").Error; err != nil {
			t.Fatal(err)
		}
		s.expireInboxWindow(context.Background())
		if row := irS02Load(t, db, secondID); row.ProcessedAt != nil {
			t.Fatal("missing scheduler row must fail closed, not expire")
		}
		if irS02MetricCount(s, "expiry_paused") != 2 {
			t.Fatalf("missing gate paused = %d", irS02MetricCount(s, "expiry_paused"))
		}
		irS02ResetScheduler(t, db)
		s.expireInboxWindow(context.Background())
		if !irS02Terminal(irS02Load(t, db, secondID)) {
			t.Fatal("expiry did not recover after scheduler row restored")
		}
		t.Log("history_enabled=false and missing scheduler row both fail closed; true restores expiry")
	})

	t.Run("T14_sustained_drain_above_historical_arrival", func(t *testing.T) {
		db, s := irS02Fixture(t)
		devices := []model.Device{
			irS02Device(t, db, "IR-S02-T14-A"),
			irS02Device(t, db, "IR-S02-T14-B"),
			irS02Device(t, db, "IR-S02-T14-C"),
			irS02Device(t, db, "IR-S02-T14-D"),
		}
		now := time.Now().UTC()
		oldTS := now.Add(-8 * 24 * time.Hour).UnixMilli()
		oldReceived := now.Add(-8 * 24 * time.Hour)
		const initialPerDevice = 500
		for _, d := range devices {
			irS02BulkOld(t, db, d, "ir-s02-sustained-initial", 1, initialPerDevice, oldTS, oldReceived)
		}
		ctx, cancel := context.WithCancel(context.Background())
		done := make(chan struct{})
		go func() {
			defer close(done)
			s.expiryWorker(ctx)
		}()
		defer func() {
			cancel()
			<-done
		}()

		// 25 行/秒 = 90,000 行/小时，略高于 96 记录的 83,318 行/小时；用“旧 ts、
		// 新接收”验证窗口按 ts_ms 而不是 received_at，且持续输入下仍有净排水。
		arrivals := 1500
		arrivalErr := make(chan error, 1)
		go func() {
			ticker := time.NewTicker(40 * time.Millisecond)
			defer ticker.Stop()
			for i := 0; i < arrivals; i++ {
				<-ticker.C
				d := devices[i%len(devices)]
				if err := db.Exec(`INSERT INTO telemetry_inbox(run_id,device_id,seq,ts_ms,payload,received_at,business_state)
VALUES('ir-s02-sustained-arrival',?,?,?, '{}'::jsonb,now(),'pending')`, d.ID, 10000+i, oldTS).Error; err != nil {
					arrivalErr <- err
					return
				}
			}
			arrivalErr <- nil
		}()

		start := time.Now()
		deadline := start.Add(110 * time.Second)
		for {
			pending := irS02PendingCount(t, db)
			if pending == 0 {
				break
			}
			if time.Now().After(deadline) {
				t.Fatalf("sustained drain left %d pending after 110s", pending)
			}
			time.Sleep(250 * time.Millisecond)
		}
		elapsed := time.Since(start)
		if err := <-arrivalErr; err != nil {
			t.Fatal(err)
		}
		total := int64(len(devices)*initialPerDevice + arrivals)
		rows := int64(irS02MetricCount(s, "expiry_rows"))
		if rows < total {
			t.Fatalf("expiry_rows = %d, want at least %d", rows, total)
		}
		rate := float64(total) / elapsed.Seconds() * 3600
		if rate < 120000 {
			t.Fatalf("observed sustained drain rate %.0f rows/h, want >=120000", rate)
		}
		t.Logf("sustained drain: %d rows in %s (%.0f rows/h), arrivals 25/s=90k/h, frozen %d/%s", total, elapsed, rate, inboxExpiryBatch, inboxExpiryInterval)
	})

	t.Run("T15_expiry_does_not_hold_scheduler_lock", func(t *testing.T) {
		db, s := irS02Fixture(t)
		d := irS02Device(t, db, "IR-S02-T15")
		now := time.Now().UTC()
		oldTS := now.Add(-8 * 24 * time.Hour).UnixMilli()
		const rows = 50
		irS02BulkOld(t, db, d, "ir-s02-lock", 1, rows, oldTS, now.Add(-8*24*time.Hour))
		if err := db.Exec(`CREATE OR REPLACE FUNCTION ir_s02_lock_slow() RETURNS trigger AS $$
BEGIN
 PERFORM pg_sleep(0.05);
 RETURN NEW;
END
$$ LANGUAGE plpgsql`).Error; err != nil {
			t.Fatal(err)
		}
		if err := db.Exec("DROP TRIGGER IF EXISTS ir_s02_lock_slow ON telemetry_inbox").Error; err != nil {
			t.Fatal(err)
		}
		if err := db.Exec("CREATE TRIGGER ir_s02_lock_slow BEFORE UPDATE ON telemetry_inbox FOR EACH ROW EXECUTE FUNCTION ir_s02_lock_slow()").Error; err != nil {
			t.Fatal(err)
		}
		defer func() {
			db.Exec("DROP TRIGGER IF EXISTS ir_s02_lock_slow ON telemetry_inbox")
			db.Exec("DROP FUNCTION IF EXISTS ir_s02_lock_slow()")
		}()
		finished := make(chan struct{})
		go func() {
			defer close(finished)
			s.expireInboxWindow(context.Background())
		}()
		time.Sleep(250 * time.Millisecond)
		if !s.scheduler.mu.TryLock() {
			<-finished
			t.Fatal("expiry holds scheduler.mu; realtime tick would be blocked")
		}
		s.scheduler.mu.Unlock()
		select {
		case <-finished:
		case <-time.After(10 * time.Second):
			t.Fatal("slow expiry did not finish")
		}
		if got := irS02PendingCount(t, db); got != 0 {
			t.Fatalf("pending after slow expiry = %d", got)
		}
		t.Log("scheduler.mu was free while expiry was mid-batch; realtime path is not locked out")
	})

	t.Run("T10_no_energy_or_td_side_effects", func(t *testing.T) {
		db, s := irS02Fixture(t)
		d := irS02Device(t, db, "IR-S02-T10")
		now := time.Now().UTC()
		oldTS := now.Add(-8 * 24 * time.Hour).UnixMilli()
		id := irS02Insert(t, db, d, 1, oldTS, now.Add(-8*24*time.Hour), irS02Options{})
		day := time.UnixMilli(oldTS).In(time.FixedZone("Asia/Shanghai", 8*3600)).Format("2006-01-02")
		if err := db.Exec(`INSERT INTO energy_daily(device_id,day,kwh,segments_count,source_quality,covered_ms,gap_count,max_gap_ms,updated_at)
VALUES(?,?,13.7,1,'verified',1000,0,0,now())`, d.ID, day).Error; err != nil {
			t.Fatal(err)
		}
		if err := db.Exec(`INSERT INTO energy_dirty_days(device_id,day,changed_at) VALUES(?,?,now())`, d.ID, day).Error; err != nil {
			t.Fatal(err)
		}
		var before struct {
			KWh           float64
			SourceQuality string
			UpdatedAt     time.Time
		}
		if err := db.Raw(`SELECT kwh,source_quality,updated_at FROM energy_daily WHERE device_id=? AND day=?`, d.ID, day).Scan(&before).Error; err != nil {
			t.Fatal(err)
		}
		var dirtyBefore, snapshotsBefore, progressBefore int64
		db.Raw(`SELECT count(*) FROM energy_dirty_days WHERE device_id=? AND day=?`, d.ID, day).Scan(&dirtyBefore)
		db.Raw(`SELECT count(*) FROM telemetry_snapshots WHERE device_id=?`, d.ID).Scan(&snapshotsBefore)
		db.Raw(`SELECT count(*) FROM telemetry_alarm_progress WHERE device_id=?`, d.ID).Scan(&progressBefore)

		s.expireInboxWindow(context.Background())

		var after struct {
			KWh           float64
			SourceQuality string
			UpdatedAt     time.Time
		}
		if err := db.Raw(`SELECT kwh,source_quality,updated_at FROM energy_daily WHERE device_id=? AND day=?`, d.ID, day).Scan(&after).Error; err != nil {
			t.Fatal(err)
		}
		if after.KWh != before.KWh || after.SourceQuality != before.SourceQuality || !after.UpdatedAt.Equal(before.UpdatedAt) {
			t.Fatalf("energy_daily changed: before=%+v after=%+v", before, after)
		}
		var dirtyAfter, snapshotsAfter, progressAfter int64
		db.Raw(`SELECT count(*) FROM energy_dirty_days WHERE device_id=? AND day=?`, d.ID, day).Scan(&dirtyAfter)
		db.Raw(`SELECT count(*) FROM telemetry_snapshots WHERE device_id=?`, d.ID).Scan(&snapshotsAfter)
		db.Raw(`SELECT count(*) FROM telemetry_alarm_progress WHERE device_id=?`, d.ID).Scan(&progressAfter)
		if dirtyAfter != dirtyBefore || snapshotsAfter != snapshotsBefore || progressAfter != progressBefore {
			t.Fatalf("side effects: dirty %d->%d snapshots %d->%d progress %d->%d", dirtyBefore, dirtyAfter, snapshotsBefore, snapshotsAfter, progressBefore, progressAfter)
		}
		if !irS02Terminal(irS02Load(t, db, id)) {
			t.Fatal("expiry did not run")
		}
		t.Log("energy_daily kwh/source_quality/updated_at, dirty marker, snapshots and alarm progress unchanged")
	})

	t.Run("T09_query_plans_with_default_planner", func(t *testing.T) {
		db, _ := irS02Fixture(t)
		now := time.Now().UTC()
		floor := now.Add(-inboxWindow).UnixMilli()
		oldTS := now.Add(-8 * 24 * time.Hour).UnixMilli()
		recentTS := now.Add(-time.Minute).UnixMilli()
		// 143 台设备，每台 700 条窗外 + 700 条窗口内，约 20 万行；不做
		// enable_seqscan=off，让默认 planner 自己选。
		const devices = 143
		const perDevice = 700
		for i := 0; i < devices; i++ {
			d := irS02Device(t, db, fmt.Sprintf("IR-S02-T09-%03d", i))
			irS02BulkOld(t, db, d, "ir-s02-plan-old", 1, perDevice, oldTS, now.Add(-8*24*time.Hour))
			irS02BulkOld(t, db, d, "ir-s02-plan-new", 1, perDevice, recentTS, now.Add(-time.Minute))
		}
		// 再压入 200 万条窗口内 pending，把 pending 集合推到 220 万量级，接近
		// 生产积压时的计划选择；这些行是窗口内，不会被过期选中。
		if err := db.Exec(`INSERT INTO telemetry_inbox(run_id,device_id,seq,ts_ms,payload,received_at,business_state)
SELECT 'ir-s02-plan-bulk',(SELECT min(id) FROM devices),g,?, '{}'::jsonb,now(),'pending' FROM generate_series(1,2000000) g`, recentTS).Error; err != nil {
			t.Fatal(err)
		}
		if err := db.Exec("ANALYZE telemetry_inbox").Error; err != nil {
			t.Fatal(err)
		}
		if err := db.Exec("ANALYZE devices").Error; err != nil {
			t.Fatal(err)
		}

		drainPlan, drainElapsed := irS02Plan(t, db, buildInboxExpiryQuery(), floor, inboxExpiryBatch)
		t.Logf("drain plan (%s): %s", drainElapsed, strings.Join(drainPlan, " | "))
		if drainElapsed >= inboxExpiryBudget {
			t.Fatalf("drain plan took %s, over %s budget at test scale", drainElapsed, inboxExpiryBudget)
		}
		drainJoined := strings.Join(drainPlan, " ")
		if !strings.Contains(drainJoined, "Nested Loop") || !strings.Contains(drainJoined, "Index Cond: (id = expired.id)") {
			t.Fatalf("drain UPDATE is not bounded by expired ids; planner may scan the pending set: %v", drainPlan)
		}

		// 无候选的最坏情况：所有 pending 都在窗口内。默认 planner 可能全扫
		// pending 集合；这正是生产启用前要 EXPLAIN 或 021 的条件。
		noMatchFloor := now.Add(-365 * 24 * time.Hour).UnixMilli()
		noMatchPlan, noMatchElapsed := irS02Plan(t, db, `SELECT id FROM telemetry_inbox WHERE processed_at IS NULL AND ts_ms<? ORDER BY id ASC LIMIT ?`, noMatchFloor, inboxExpiryBatch)
		t.Logf("no-candidate plan (%s): %s", noMatchElapsed, strings.Join(noMatchPlan, " | "))

		headPlan, headElapsed := irS02Plan(t, db, inboxHeadAgeQuery, now, inboxHeadAgeDeviceLimit, floor)
		t.Logf("head plan (%s): %s", headElapsed, strings.Join(headPlan, " | "))
		joined := strings.Join(headPlan, " ")
		if !strings.Contains(joined, "idx_inbox_pending_device") {
			t.Fatalf("head plan does not use idx_inbox_pending_device: %v", headPlan)
		}
	})
}
