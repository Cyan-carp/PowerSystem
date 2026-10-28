//go:build tr_s03 && tr_s02

package api

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/redis/go-redis/v9"
	"go.uber.org/zap"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"powersystem/backend/internal/config"
	"powersystem/backend/internal/model"
	"powersystem/backend/internal/service"
	"powersystem/backend/internal/store"
	"powersystem/backend/internal/tdengine"
	"powersystem/backend/internal/telemetry"
)

const s03DSN = "postgres://tr_s03@127.0.0.1:15441/tr_s03_test?sslmode=disable"

type s03Fixture struct {
	t         *testing.T
	db        *gorm.DB
	rd        *redis.Client
	s         *Server
	mode      atomic.Int32
	missing   atomic.Int64
	calls     atomic.Int64
	callMu    sync.Mutex
	callTimes []time.Time
}

func s03new(t *testing.T) *s03Fixture {
	t.Helper()
	if os.Getenv("TR_S03_TEST_DSN") != s03DSN {
		t.Fatal("dedicated S03 fixture required; no SKIP")
	}
	f := &s03Fixture{t: t}
	var err error
	f.db, err = gorm.Open(postgres.Open(s03DSN), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	s02must(t, err)
	p, _ := f.db.DB()
	t.Cleanup(func() { p.Close() })
	s02must(t, f.db.Exec(`DROP SCHEMA public CASCADE; CREATE SCHEMA public`).Error)
	// Preserve every scenario's synthetic rows separately before the next reset.
	t.Cleanup(func() {
		name := fmt.Sprintf("retained_%s_%d", strings.ToLower(t.Name()), time.Now().UnixMilli())
		var tables []string
		if e := f.db.Raw("SELECT tablename FROM pg_tables WHERE schemaname='public'").Scan(&tables).Error; e != nil {
			t.Error(e)
			return
		}
		if e := f.db.Exec("CREATE SCHEMA " + name).Error; e != nil {
			t.Error(e)
			return
		}
		for _, table := range tables {
			if e := f.db.Exec("CREATE TABLE " + name + ".\"" + table + "\" AS TABLE public.\"" + table + "\"").Error; e != nil {
				t.Error(e)
			}
		}
	})
	// Test-only schema reset; migrations themselves use the real API entry below.
	bin := os.Getenv("TR_S03_API_BIN")
	if bin == "" {
		t.Fatal("actual API binary required")
	}
	for n := 0; n < 2; n++ {
		c := exec.Command(bin, "-migrate-only")
		c.Dir = "../.."
		c.Env = append(os.Environ(), "POSTGRES_HOST=127.0.0.1", "POSTGRES_PORT=15441", "POSTGRES_USER=tr_s03", "POSTGRES_DB=tr_s03_test", "POSTGRES_PASSWORD=fixture-only", "REDIS_PASSWORD=fixture-only", "TDENGINE_ROOT_PASSWORD=fixture-only", "JWT_SECRET="+strings.Repeat("t", 32), "SIMULATION_ALARM_MODE=off", "AGENT_ENABLED=false", "FEISHU_BUSINESS_ENABLED=false")
		b, e := c.CombinedOutput()
		if e != nil || !strings.Contains(string(b), "migration_complete") {
			t.Fatalf("actual migration entry: %v %s", e, b)
		}
	}
	f.rd = redis.NewClient(&redis.Options{Addr: "127.0.0.1:16391", ContextTimeoutEnabled: true, MaxRetries: -1})
	t.Cleanup(func() { f.rd.Close() })
	s02must(t, f.rd.Ping(context.Background()).Err())
	s02must(t, f.rd.FlushDB(context.Background()).Err())
	td := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		f.calls.Add(1)
		f.callMu.Lock()
		f.callTimes = append(f.callTimes, time.Now())
		f.callMu.Unlock()
		q, _ := io.ReadAll(r.Body)
		switch f.mode.Load() {
		case 1:
			http.Error(w, "fixture dependency failure", 503)
			return
		case 2:
			select {
			case <-r.Context().Done():
				return
			case <-time.After(2 * time.Second):
			}
		}
		data := [][]any{{1}}
		if f.missing.Load() > 0 && strings.Contains(string(q), fmt.Sprintf("ts=%d ", f.missing.Load())) {
			data = nil
		}
		json.NewEncoder(w).Encode(map[string]any{"code": 0, "data": data})
	}))
	t.Cleanup(td.Close)
	cfg := config.Config{TDURL: td.URL, TDDatabase: "tr_s03", SimulationAlarmMode: "off", JWTSecret: strings.Repeat("x", 32)}
	f.s = New(cfg, f.db, f.rd, tdengine.New(cfg), zap.NewNop())
	t.Cleanup(func() {
		if f.s.realtime.cancel != nil {
			f.s.realtime.cancel()
			<-f.s.realtime.done
		}
	})
	return f
}
func (f *s03Fixture) device(code string) model.Device {
	d := model.Device{DeviceCode: code, Name: "S03 fixture", DevType: "inverter", StationCode: "S03"}
	s02must(f.t, f.db.Create(&d).Error)
	return d
}
func (f *s03Fixture) item(d model.Device, run string, seq int64) model.Inbox {
	sample := telemetry.Sample{SchemaVersion: 1, DeviceCode: d.DeviceCode, StationCode: d.StationCode, RunID: run, Seq: seq, TS: time.Now().Add(-time.Second).UnixMilli() + seq, Temperature: 60, Voltage: 400, Current: 100, Power: 68.589211, Status: 1}
	payload, _ := json.Marshal(sample)
	s02must(f.t, f.db.Exec(`INSERT INTO telemetry_inbox(run_id,device_id,seq,ts_ms,payload,received_at) VALUES(?,?,?,?,?::jsonb,?) ON CONFLICT(run_id,device_id,seq) DO NOTHING`, run, d.ID, seq, sample.TS, string(payload), time.Now().Add(-time.Second)).Error)
	var row model.Inbox
	s02must(f.t, f.db.Where("device_id=? AND run_id=? AND seq=?", d.ID, run, seq).First(&row).Error)
	return row
}
func (f *s03Fixture) row(id int64) model.Inbox {
	var r model.Inbox
	s02must(f.t, f.db.First(&r, id).Error)
	return r
}
func (f *s03Fixture) due(id int64) {
	s02must(f.t, f.db.Exec(`UPDATE telemetry_inbox SET next_attempt_at=? WHERE id=?`, time.Now().Add(-time.Second), id).Error)
}
func (f *s03Fixture) state(id int64) string {
	r := f.row(id)
	if r.BusinessState == nil {
		return "legacy"
	}
	return *r.BusinessState
}
func (f *s03Fixture) count(table string) int64 {
	var n int64
	s02must(f.t, f.db.Table(table).Count(&n).Error)
	return n
}
func (f *s03Fixture) rule(d model.Device) {
	s02must(f.t, f.db.Create(&model.AlarmRule{DeviceID: d.ID, Metric: "temperature", Operator: ">", Threshold: 50, Level: "major", Enabled: true, Version: "1"}).Error)
}
func TestTRS03T01(t *testing.T) {
	f := s03new(t)
	var high model.Inbox
	for n := 0; n < 501; n++ {
		d := f.device(fmt.Sprintf("S03-F-%03d", n))
		r := f.item(d, "fair", 1)
		if n < 500 {
			s02must(t, f.db.Exec(`UPDATE telemetry_inbox SET business_state='retry',next_attempt_at=now()+interval '1 hour' WHERE id=?`, r.ID).Error)
		} else {
			high = r
		}
	}
	// Execute the exact S02 selection against the live PG fixture, 4 rounds.
	for n := 0; n < 4; n++ {
		var old []model.Inbox
		s02must(t, f.db.Raw(`SELECT DISTINCT ON(device_id) * FROM telemetry_inbox WHERE processed_at IS NULL AND (business_state IS NULL OR business_state IN('pending','retry')) ORDER BY device_id,id LIMIT 500`).Scan(&old).Error)
		if len(old) != 500 || old[len(old)-1].DeviceID >= high.DeviceID {
			t.Fatal("baseline reproduction invalid")
		}
	}
	t.Log("S02 query reproduced conditional starvation in 4 rounds: first 500 continuously pending/deferred; high ID never selected (500 is page size, not failure threshold)")
	start := time.Now()
	f.s.processPending(context.Background())
	if f.state(high.ID) == "success" {
		t.Fatal("expected page boundary")
	}
	f.s.processPending(context.Background())
	if f.state(high.ID) != "success" || time.Since(start) > 10*time.Second {
		t.Fatal("high ID starved")
	}
	var n int64
	f.db.Raw(`SELECT count(*) FROM telemetry_inbox WHERE processed_at IS NULL`).Scan(&n)
	if n != 500 {
		t.Fatal("backlog altered")
	}
	f.s.telemetry.mu.Lock()
	wrap := f.s.telemetry.counts["scheduler_wrap"]
	visits := f.s.telemetry.counts["scheduler_visit"]
	f.s.telemetry.mu.Unlock()
	if wrap == 0 || visits != 1000 {
		t.Fatalf("wrap/visits %d/%d", wrap, visits)
	}
	t.Logf("501 high-ID success after 2 rounds, wrap covered; 500 pending retained, 1000 visits; measured %s, budget 10s", time.Since(start))
}
func TestTRS03T02(t *testing.T) {
	f := s03new(t)
	var ids []int64
	for n := 0; n < 143; n++ {
		d := f.device(fmt.Sprintf("S03-B-%03d", n))
		r := f.item(d, "budget", 1)
		ids = append(ids, r.ID)
	}
	// Keep rows pending after success? Terminal rows do not need more scheduling.
	// Slow lowest IDs repeatedly exceed the 120ms round, so progress must resume.
	s02must(t, f.db.Exec(fmt.Sprintf(`CREATE FUNCTION s03_slow() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.id<=%d AND NEW.td_confirmed_at IS NOT NULL THEN PERFORM pg_sleep(0.3); END IF; RETURN NEW; END $$; CREATE TRIGGER s03_slow BEFORE UPDATE ON telemetry_inbox FOR EACH ROW EXECUTE FUNCTION s03_slow()`, ids[4])).Error)
	s02must(t, f.db.Exec(`CREATE TABLE s03_visits(device_id bigint,at timestamptz DEFAULT clock_timestamp()); CREATE FUNCTION s03_visit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN INSERT INTO s03_visits(device_id) VALUES(NEW.last_device_id); RETURN NEW; END $$; CREATE TRIGGER s03_visit AFTER UPDATE ON telemetry_scheduler FOR EACH ROW EXECUTE FUNCTION s03_visit()`).Error)
	start := time.Now()
	rounds := 0
	for ; rounds < 80; rounds++ {
		f.s.processPendingBudget(context.Background(), 120*time.Millisecond)
		var n int64
		f.db.Raw(`SELECT count(DISTINCT device_id) FROM s03_visits`).Scan(&n)
		if n == 143 {
			break
		}
	}
	if rounds >= 80 || time.Since(start) > 20*time.Second {
		t.Fatal("starvation / fairness budget")
	}
	var max struct{ Gap float64 }
	s02must(t, f.db.Raw(`SELECT EXTRACT(EPOCH FROM max(at)-min(at)) AS gap FROM s03_visits`).Scan(&max).Error)
	// Refill terminal heads as next rows, preserving the first slow heads.
	var devices []model.Device
	s02must(t, f.db.Where("station_code='S03'").Order("id").Find(&devices).Error)
	for _, d := range devices {
		f.item(d, "budget", 2)
	}
	until := time.Now().Add(20 * time.Second)
	for time.Now().Before(until) {
		f.s.processPendingBudget(context.Background(), 120*time.Millisecond)
		var n int64
		f.db.Raw(`SELECT count(*) FROM (SELECT device_id FROM s03_visits GROUP BY device_id HAVING count(*)>=2) x`).Scan(&n)
		if n == 143 {
			break
		}
	}
	var n int64
	f.db.Raw(`SELECT count(*) FROM (SELECT device_id FROM s03_visits GROUP BY device_id HAVING count(*)>=2) x`).Scan(&n)
	if n != 143 {
		t.Fatal("no second service for every device")
	}
	var worst float64
	s02must(t, f.db.Raw(`SELECT COALESCE(EXTRACT(EPOCH FROM max(gap)),0) FROM (SELECT at-lag(at) OVER(PARTITION BY device_id ORDER BY at) gap FROM s03_visits) x`).Scan(&worst).Error)
	if worst > 20 {
		t.Fatal("worst service interval exceeded")
	}
	f.s.telemetry.mu.Lock()
	truncated := f.s.telemetry.counts["scheduler_budget"]
	f.s.telemetry.mu.Unlock()
	if truncated < 2 {
		t.Fatal("budget truncation not reproduced")
	}
	t.Logf("143 devices, 120ms rounds, 300ms low-ID SQL delays; %d initial rounds, %d truncations; first sweep %.3fs, worst repeated service interval %.3fs <=20s", rounds+1, truncated, max.Gap, worst)
}
func TestTRS03T03(t *testing.T) {
	f := s03new(t)
	d := f.device("S03-MISSING")
	head := f.item(d, "missing", 1)
	next := f.item(d, "missing", 2)
	other := f.item(f.device("S03-OTHER"), "other", 1)
	f.missing.Store(head.TS)
	f.s.processPending(context.Background())
	if f.state(head.ID) != "retry" || f.state(next.ID) != "pending" || f.state(other.ID) != "success" {
		t.Fatal("missing isolation boundary")
	}
	r := f.row(head.ID)
	if r.FirstFailedAt == nil || r.MissingAttempts != 1 || r.NextAttemptAt == nil {
		t.Fatal("missing retry not durable")
	}
	before := f.calls.Load()
	f.s.processPending(context.Background())
	if f.calls.Load() != before {
		t.Fatal("busy poll")
	}
	// Synthetic database policy clock; no real 10-minute capacity run.
	for n := 0; n < 5; n++ {
		f.due(head.ID)
		f.s.processPending(context.Background())
	}
	if f.state(head.ID) != "retry" {
		t.Fatal("attempts alone quarantined")
	}
	s02must(t, f.db.Exec(`UPDATE telemetry_inbox SET missing_since=?,missing_checked_at=? WHERE id=?`, time.Now().Add(-tdMissingGrace-time.Second), time.Now(), head.ID).Error)
	f.due(head.ID)
	f.s.processPending(context.Background())
	if f.state(head.ID) != "quarantined" || f.state(next.ID) != "pending" {
		t.Fatalf("confirmed quarantine: head=%+v successor=%s", f.row(head.ID), f.state(next.ID))
	}
	f.s.processPending(context.Background())
	if f.state(next.ID) != "success" {
		t.Fatal("successor blocked")
	}
	r = f.row(head.ID)
	if r.FirstFailedAt == nil || r.Attempts < 7 || len(r.Payload) == 0 || r.FailureCode == nil || *r.FailureCode != "td_missing" {
		t.Fatal("audit missing")
	}
	gap := f.item(f.device("S03-MISSING-GAP"), "gap", 1)
	f.missing.Store(gap.TS)
	s02must(t, f.db.Exec(`UPDATE telemetry_inbox SET business_state='retry',missing_since=?,missing_checked_at=?,missing_attempts=5 WHERE id=?`, time.Now().Add(-11*time.Minute), time.Now().Add(-3*time.Minute), gap.ID).Error)
	f.s.processInboxItem(context.Background(), gap)
	if f.state(gap.ID) != "retry" || f.row(gap.ID).MissingAttempts != 1 {
		t.Fatal("unobserved interval charged as continuous missing")
	}
	t.Log("10-minute confirmed-missing grace + >=6 checks, separate clock/gap and first-failure counters; grace/attempt predicates independent; original head retained and successor advances only after quarantine")
}
func TestTRS03T04(t *testing.T) {
	f := s03new(t)
	var rows []model.Inbox
	for n := 0; n < 12; n++ {
		rows = append(rows, f.item(f.device(fmt.Sprintf("S03-D-%02d", n)), "deps", 1))
	}
	// Reception age cannot cause quarantine even after a day.
	s02must(t, f.db.Exec(`UPDATE telemetry_inbox SET received_at=now()-interval '1 day'`).Error)
	f.mode.Store(1)
	f.s.processPending(context.Background())
	initial := f.calls.Load()
	for n := 0; n < 5; n++ {
		f.s.processPending(context.Background())
	}
	if initial < 2 {
		t.Fatal("TD breaker not exercised")
	}
	// A round can itself outlast cooldown. Further attempts are valid half-open
	// probes; assert their elapsed spacing rather than a fixed per-round count.
	f.callMu.Lock()
	times := append([]time.Time(nil), f.callTimes...)
	f.callMu.Unlock()
	for n := 2; n < len(times); n++ {
		if times[n].Sub(times[n-1]) < retryDelay(int64(n-1), time.Second, 30*time.Second) {
			t.Fatal("busy dependency polling during cooldown")
		}
	}
	failedCalls := f.calls.Load()
	for _, r := range rows {
		if f.state(r.ID) == "quarantined" {
			t.Fatal("dependency quarantined")
		}
	}
	f.mode.Store(0)
	f.s.tdCircuit.mu.Lock()
	wait := time.Until(f.s.tdCircuit.until)
	f.s.tdCircuit.mu.Unlock()
	if wait > 0 {
		time.Sleep(wait + 10*time.Millisecond)
	}
	f.s.processPending(context.Background())
	if f.calls.Load() <= failedCalls {
		t.Fatal("no half-open recovery probe")
	}
	for _, r := range rows {
		f.due(r.ID)
	}
	f.s.processPending(context.Background())
	for _, r := range rows {
		if f.state(r.ID) != "success" {
			t.Fatal("TD recovery failed")
		}
	}
	// Real Redis client pointed at an intentionally unused loopback port.
	bad := redis.NewClient(&redis.Options{Addr: "127.0.0.1:16392", ContextTimeoutEnabled: true, MaxRetries: -1, DialTimeout: 100 * time.Millisecond})
	defer bad.Close()
	good := f.s.redis
	f.s.redis = bad
	var second []model.Inbox
	for n := 0; n < 8; n++ {
		second = append(second, f.item(f.device(fmt.Sprintf("S03-R-%02d", n)), "redis", 1))
	}
	f.s.processPending(context.Background())
	f.s.processPending(context.Background())
	f.s.telemetry.mu.Lock()
	attempts := f.s.telemetry.counts["redis_circuit_attempt"]
	f.s.telemetry.mu.Unlock()
	if attempts != 14 {
		t.Fatalf("Redis attempts after 12 good+2 bad: %d", attempts)
	}
	f.s.redis = good
	time.Sleep(1100 * time.Millisecond)
	for _, r := range second {
		f.due(r.ID)
	}
	f.s.processPending(context.Background())
	for _, r := range second {
		if f.state(r.ID) != "success" {
			t.Fatalf("Redis recovery failed: row=%+v circuit=%+v", f.row(r.ID), f.s.redisCircuit)
		}
	}
	// A dependency error resets confirmed-missing streak, not normal data.
	d := f.device("S03-RESET")
	r := f.item(d, "reset", 1)
	f.missing.Store(r.TS)
	f.s.processInboxItem(context.Background(), r)
	f.mode.Store(1)
	f.due(r.ID)
	f.s.processInboxItem(context.Background(), r)
	row := f.row(r.ID)
	if row.MissingAttempts != 0 || row.MissingSince != nil {
		t.Fatal("dependency charged as missing")
	}
	// Two genuinely slow requests open the same circuit within 2 dependency budgets.
	f.s.tdCircuit.mu.Lock()
	f.s.tdCircuit.failures = 0
	f.s.tdCircuit.until = time.Time{}
	f.s.tdCircuit.mu.Unlock()
	f.mode.Store(2)
	f.missing.Store(0)
	for n := 0; n < 4; n++ {
		f.item(f.device(fmt.Sprintf("S03-SLOW-%d", n)), "slow", 1)
	}
	before := f.calls.Load()
	start := time.Now()
	f.s.processPending(context.Background())
	if f.calls.Load()-before != 2 || time.Since(start) > 3*time.Second {
		t.Fatal("slow dependencies exhausted round / circuit failed")
	}
	t.Log("sustained TD/Redis outages bounded to 2 initial attempts, cooldown skips, one half-open probe restores; old reception ages retained, no false quarantine; missing streak reset on dependency error")
}
func TestTRS03T05(t *testing.T) {
	f := s03new(t)
	d := f.device("S03-IDEMPOTENT")
	f.rule(d)
	a := f.item(d, "run-a", 1)
	again := f.item(d, "run-a", 1)
	b := f.item(d, "run-b", 1)
	if a.ID != again.ID {
		t.Fatal("duplicate MQTT tuple")
	}
	// Real ingestion callback twice, not just insertion SQL.
	sample := telemetry.Sample{SchemaVersion: 1, DeviceCode: d.DeviceCode, StationCode: d.StationCode, RunID: "mqtt", Seq: 1, TS: time.Now().UnixMilli(), Temperature: 60, Voltage: 400, Current: 100, Power: 68.589211, Status: 1}
	payload, _ := json.Marshal(sample)
	f.s.receive(nil, s03Message{payload})
	f.s.receive(nil, s03Message{payload})
	if f.count("telemetry_inbox") != 3 {
		t.Fatal("duplicate MQTT accepted")
	}
	f.s.processInboxItem(context.Background(), b)
	if f.state(b.ID) != "pending" {
		t.Fatal("cross-run bypass")
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	f.s.processInboxItem(ctx, a)
	if f.state(a.ID) != "pending" || f.row(a.ID).Attempts != 0 {
		t.Fatal("known cancellation counted as failure")
	}
	f.s.processInboxItem(context.Background(), a)
	f.s.processInboxItem(context.Background(), a)
	f.s.processInboxItem(context.Background(), b)
	f.s.processPending(context.Background())
	if f.count("alarm_records") != 1 || f.count("business_notification_outbox") != 1 {
		t.Fatal("duplicate business/outbox")
	}
	if f.state(a.ID) != "success" || f.state(b.ID) != "success" {
		t.Fatal("cross-run progression")
	}
	t.Log("actual duplicate ingestion, same seq across run IDs, early successor attempt and cancel/retry interleaving: three durable rows, one alarm/outbox, no partial transaction")
}

type s03Message struct{ payload []byte }

func (s03Message) Duplicate() bool   { return true }
func (s03Message) Qos() byte         { return 1 }
func (s03Message) Retained() bool    { return false }
func (s03Message) Topic() string     { return "telemetry/S03" }
func (s03Message) MessageID() uint16 { return 1 }
func (m s03Message) Payload() []byte { return m.payload }
func (s03Message) Ack()              {}
func TestTRS03T06(t *testing.T) {
	f := s03new(t)
	d := f.device("S03-DELETED")
	r := f.item(d, "deleted", 1)
	next := f.item(d, "deleted", 2)
	s02must(t, f.db.Exec(`UPDATE devices SET deleted_at=now() WHERE id=?`, d.ID).Error)
	silent := f.device("S03-SILENT")
	sr := f.item(silent, "silent", 1)
	s02must(t, f.db.Exec(`INSERT INTO telemetry_silence_windows(device_id,starts_at,ends_at,reason,created_by) VALUES(?,now()-interval '1 minute',now()+interval '1 hour','explicit maintenance','fixture')`, silent.ID).Error)
	f.s.processPending(context.Background())
	if f.state(r.ID) != "quarantined" || f.state(sr.ID) != "success" {
		t.Fatal("deleted/silent handling")
	}
	ds, _, err := f.s.expectedTelemetryDevices(context.Background(), time.Now())
	s02must(t, err)
	found := false
	for _, v := range ds {
		if v.ID == d.ID {
			t.Fatal("deleted expected")
		}
		if v.ID == silent.ID {
			found = v.Silent
		}
	}
	if !found {
		t.Fatal("silent audit lost")
	}
	fresh := f.device("S03-ADDED")
	nr := f.item(fresh, "new", 1)
	s02must(t, f.db.Exec(`CREATE TABLE s03_restart_visits(id bigserial,device_id bigint); CREATE FUNCTION s03_restart_visit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN INSERT INTO s03_restart_visits(device_id) VALUES(NEW.last_device_id); RETURN NEW; END $$; CREATE TRIGGER s03_restart_visit AFTER UPDATE ON telemetry_scheduler FOR EACH ROW EXECUTE FUNCTION s03_restart_visit()`).Error)
	// Simulate fresh Server with the same persistent scheduler row.
	restarted := New(f.s.cfg, f.db, f.rd, f.s.td, zap.NewNop())
	restarted.processPending(context.Background())
	var firstVisit int64
	s02must(t, f.db.Raw(`SELECT device_id FROM s03_restart_visits ORDER BY id LIMIT 1`).Scan(&firstVisit).Error)
	if firstVisit != fresh.ID {
		t.Fatal("restart reset durable cursor")
	}
	t.Cleanup(func() {
		if restarted.realtime.cancel != nil {
			restarted.realtime.cancel()
			<-restarted.realtime.done
		}
	})
	if f.state(next.ID) != "quarantined" || f.state(nr.ID) != "success" {
		t.Fatal("restart/new/wrap")
	}
	restarted.processPending(context.Background())
	var pending int64
	f.db.Raw(`SELECT count(*) FROM telemetry_inbox WHERE processed_at IS NULL`).Scan(&pending)
	if pending != 0 {
		t.Fatal("deleted blocks forever")
	}
	t.Log("new registry device, soft deletion, audited explicit silence, tail wrap and fresh Server restart; silence only changes expectation, incoming rows retained/processed; no queue clearing or success-time rewrite")
}
func TestTRS03T07(t *testing.T) {
	f := s03new(t)
	bad := f.item(f.device("S03-BAD"), "bad", 1)
	s02must(t, f.db.Exec(`UPDATE telemetry_inbox SET payload='{}' WHERE id=?`, bad.ID).Error)
	f.s.processInboxItem(context.Background(), bad)
	if f.state(bad.ID) != "retry" {
		t.Fatal("bad grace absent")
	}
	f.due(bad.ID)
	f.s.processInboxItem(context.Background(), bad)
	if f.state(bad.ID) != "retry" {
		t.Fatal("bad attempt-only isolation")
	}
	s02must(t, f.db.Exec(`UPDATE telemetry_inbox SET reason_since=? WHERE id=?`, time.Now().Add(-payloadGrace-time.Second), bad.ID).Error)
	f.due(bad.ID)
	f.s.processInboxItem(context.Background(), bad)
	id := f.device("S03-IDENTITY")
	identity := f.item(id, "id", 1)
	s02must(t, f.db.Exec(`UPDATE devices SET station_code='CHANGED' WHERE id=?`, id.ID).Error)
	f.s.processInboxItem(context.Background(), identity)
	d := f.device("S03-BUSINESS")
	business := f.item(d, "business", 1)
	f.rule(d)
	s02must(t, f.db.Exec(`CREATE FUNCTION s03_business_fail() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'fixture failure'; END $$; CREATE TRIGGER s03_business_fail BEFORE INSERT ON alarm_records FOR EACH ROW EXECUTE FUNCTION s03_business_fail()`).Error)
	f.s.processInboxItem(context.Background(), business)
	if f.state(business.ID) != "retry" || f.count("alarm_records") != 0 || f.count("business_notification_outbox") != 0 {
		t.Fatal("business failure partial commit")
	}
	s02must(t, f.db.Exec(`DROP TRIGGER s03_business_fail ON alarm_records`).Error)
	f.due(business.ID)
	f.s.processInboxItem(context.Background(), business)
	if f.state(bad.ID) != "quarantined" || f.state(identity.ID) != "quarantined" || f.state(business.ID) != "success" {
		t.Fatalf("classification bad=%+v identity=%+v business=%+v", f.row(bad.ID), f.row(identity.ID), f.row(business.ID))
	}
	f.s.telemetry.mu.Lock()
	retry, q, success := f.s.telemetry.counts["retry"], f.s.telemetry.counts["quarantine"], f.s.telemetry.counts["success"]
	f.s.telemetry.mu.Unlock()
	if retry != 3 || q != 2 || success != 1 {
		t.Fatalf("durable transition metrics %d/%d/%d", retry, q, success)
	}
	if f.row(bad.ID).Attempts != 3 || f.row(identity.ID).Attempts != 1 || f.row(business.ID).Attempts != 1 {
		t.Fatal("attempt ledger")
	}
	t.Log("payload grace30s/3 attempts, definitive identity quarantine and retriable business rollback tracked; retry3/quarantine2/success1 reconcile with ledger and retained first failure/payload")
}
func TestTRS03T08(t *testing.T) {
	f := s03new(t)
	sql.Register("s03-unknown", &s02Driver{})
	wrapped, e := sql.Open("s03-unknown", s03DSN)
	s02must(t, e)
	defer wrapped.Close()
	wdb, e := gorm.Open(postgres.New(postgres.Config{Conn: wrapped}), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	s02must(t, e)
	for _, mode := range []int32{1, 2} {
		d := f.device(fmt.Sprintf("S03-UNKNOWN-%d", mode))
		f.rule(d)
		r := f.item(d, "unknown", 1)
		original := f.s.db
		f.s.db = wdb
		unknownMode.Store(mode)
		f.s.processInboxItem(context.Background(), r)
		f.s.db = original
		_, e = service.CompensateTelemetry(f.db, r.ID, fmt.Sprintf("inspect-%d", mode), "fixture", "resolve unknown", false)
		s02must(t, e)
		f.s.processInboxItem(context.Background(), r)
		f.s.processInboxItem(context.Background(), r)
		if f.state(r.ID) != "success" {
			t.Fatal("unknown unresolved")
		}
	}
	if f.count("alarm_records") != 2 || f.count("business_notification_outbox") != 2 {
		t.Fatal("unknown replay duplicate")
	}
	d := f.device("S03-COMPENSATE")
	f.rule(d)
	r := f.item(d, "comp", 1)
	s02must(t, f.db.Exec(`UPDATE telemetry_inbox SET business_state='quarantined',processed_at=now(),failure_code='td_missing',attempts=6,first_failed_at=now()-interval '1 hour',reason_since=now()-interval '11 minutes',reason_attempts=6,missing_since=now()-interval '11 minutes',missing_checked_at=now(),missing_attempts=6 WHERE id=?`, r.ID).Error)
	bin := os.Getenv("TR_S03_COMP_BIN")
	if bin == "" {
		t.Fatal("actual compensation binary required")
	}
	run := func(replay bool) string {
		args := []string{"-inbox-id", fmt.Sprint(r.ID), "-request-id", "s03-approved-1", "-actor", "fixture", "-reason", "isolated recovered TD row"}
		if replay {
			args = append(args, "-isolated-replay")
		}
		c := exec.Command(bin, args...)
		c.Env = append(os.Environ(), "TR_COMPENSATION_DSN="+s03DSN)
		out, err := c.CombinedOutput()
		if err != nil {
			t.Fatalf("comp CLI: %v %s", err, out)
		}
		return string(out)
	}
	if !strings.Contains(run(false), "quarantined") || f.count("telemetry_compensations") != 0 {
		t.Fatal("default replayed")
	}
	run(true)
	run(true)
	if f.count("telemetry_compensations") != 1 {
		t.Fatal("comp idempotency")
	}
	f.missing.Store(r.TS)
	f.s.processPending(context.Background())
	if f.state(r.ID) != "retry" || f.row(r.ID).MissingAttempts != 1 || f.row(r.ID).ReasonAttempts != 1 {
		t.Fatal("compensation inherited old isolation window")
	}
	f.missing.Store(0)
	f.due(r.ID)
	f.s.processPending(context.Background())
	if f.state(r.ID) != "success" || f.row(r.ID).Attempts != 7 || f.row(r.ID).FirstFailedAt == nil {
		t.Fatal("comp lost original audit")
	}
	later := f.item(d, "comp-next-run", 1)
	f.s.processPending(context.Background())
	s02must(t, f.db.Exec(`UPDATE telemetry_inbox SET business_state='quarantined',processed_at=now() WHERE id=?`, r.ID).Error)
	if _, e = service.CompensateTelemetry(f.db, r.ID, "denied-order", "fixture", "order", true); e == nil {
		t.Fatal("historical row replayed across successor")
	}
	if f.state(later.ID) != "success" || f.count("alarm_records") != 3 || f.count("business_notification_outbox") != 3 {
		t.Fatal("comp effects duplicated")
	}
	// Real concurrent worker/compensation: worker holds the device lock while
	// blocked at a rule. A compensator must wait and then observe its commit.
	cd := f.device("S03-COMP-RACE")
	f.rule(cd)
	ch := f.item(cd, "race", 1)
	cn := f.item(cd, "race-next", 1)
	s02must(t, f.db.Exec(`UPDATE telemetry_inbox SET business_state='quarantined',processed_at=now(),failure_code='td_missing' WHERE id=?`, ch.ID).Error)
	lock := f.db.Begin()
	defer lock.Rollback()
	s02must(t, lock.Exec(`SELECT id FROM alarm_rules WHERE device_id=? FOR UPDATE`, cd.ID).Error)
	done := make(chan struct{})
	go func() { f.s.processInboxItem(context.Background(), cn); close(done) }()
	s02wait(t, func() bool {
		var n int64
		f.db.Raw(`SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock' AND query LIKE '%alarm_rules%'`).Scan(&n)
		return n > 0
	})
	compDone := make(chan error, 1)
	go func() {
		_, e := service.CompensateTelemetry(f.db, ch.ID, "race-denied", "fixture", "concurrent successor", true)
		compDone <- e
	}()
	select {
	case <-compDone:
		t.Fatal("compensator bypassed device lock")
	case <-time.After(50 * time.Millisecond):
	}
	s02must(t, lock.Rollback().Error)
	<-done
	if e := <-compDone; e == nil || f.state(cn.ID) != "success" || f.state(ch.ID) != "quarantined" {
		t.Fatal("compensation reordered committed successor")
	}
	// Existing maintenance gate still fences this manual entry.
	s02must(t, f.db.Exec(`UPDATE alarm_maintenance_state SET enabled=true WHERE id=1`).Error)
	if _, e = service.CompensateTelemetry(f.db, r.ID, "denied-maintenance", "fixture", "gate", true); e == nil {
		t.Fatal("comp bypassed maintenance")
	}
	t.Log("actual CLI defaults to locked inspection; explicit isolated replay idempotent, original audit kept; successor/maintenance refusal, two actual unknown-COMMIT outcomes lock-read before retry; no dispatcher or external notification")
}

func TestTRS03PolicyBounds(t *testing.T) {
	if retryDelay(100, time.Second, time.Minute) != time.Minute || retryDelay(1, time.Second, time.Minute) != time.Second {
		t.Fatal("retry bounds")
	}
	for _, p := range store.MigrationPaths {
		if strings.HasSuffix(p, "015_telemetry_scheduler.sql") {
			return
		}
	}
	t.Fatal("migration not registered")
}
