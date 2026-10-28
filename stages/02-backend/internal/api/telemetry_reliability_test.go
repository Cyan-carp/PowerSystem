//go:build tr_s01

package api

import (
	"context"
	"encoding/json"
	"fmt"
	"github.com/redis/go-redis/v9"
	"go.uber.org/zap"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"math"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"powersystem/backend/internal/config"
	"powersystem/backend/internal/energy"
	"powersystem/backend/internal/fleet"
	"powersystem/backend/internal/model"
	"powersystem/backend/internal/store"
	"powersystem/backend/internal/tdengine"
	"powersystem/backend/internal/telemetry"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

// This test fails rather than skips without its dedicated disposable dependencies.
func TestTRS01Isolated(t *testing.T) {
	dsn := os.Getenv("TR_S01_TEST_DSN")
	if dsn != "postgres://tr_s01@127.0.0.1:15439/tr_s01_test?sslmode=disable" {
		t.Fatal("dedicated TR-S01 loopback fixture required")
	}
	db, err := gorm.Open(postgres.Open(dsn), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	mustTR(t, err)
	pool, _ := db.DB()
	defer pool.Close()
	rd := redis.NewClient(&redis.Options{Addr: "127.0.0.1:16389", DB: 0})
	defer rd.Close()
	mustTR(t, rd.Ping(context.Background()).Err())
	ctx := context.Background()
	// Install the old schema, retain an ambiguous old row, then execute the API entry.
	for _, path := range store.MigrationPaths {
		if strings.Contains(path, "014_") {
			continue
		}
		mustTR(t, store.Migrate(db, filepath.Join("../..", path)))
	}
	d := model.Device{DeviceCode: "TR-A", Name: "TR-A", DevType: "inverter", StationCode: "TR", Vendor: "fixture"}
	mustTR(t, db.Create(&d).Error)
	sample := telemetry.Sample{SchemaVersion: 1, RunID: "tr-s01", DeviceCode: d.DeviceCode, StationCode: d.StationCode, Seq: 1, TS: time.Now().UnixMilli(), Voltage: 400, Current: 100, Power: 68.589211, Temperature: 40, Status: 1}
	payload, _ := json.Marshal(sample)
	mustTR(t, db.Exec("INSERT INTO telemetry_inbox(run_id,device_id,seq,ts_ms,payload,processed_at) VALUES(?,?,?,?,?::jsonb,now())", sample.RunID, d.ID, sample.Seq, sample.TS, string(payload)).Error)
	mustTR(t, db.Exec("INSERT INTO energy_daily(device_id,day,kwh,segments_count) VALUES(?,'2000-01-01',13.7,1)", d.ID).Error)
	bin := os.Getenv("TR_S01_API_BIN")
	if bin == "" {
		t.Fatal("built API required for actual migration entry")
	}
	runMigration := func(database string) {
		cmd := exec.Command(bin, "-migrate-only")
		cmd.Dir = "../.."
		cmd.Env = append(os.Environ(), "POSTGRES_HOST=127.0.0.1", "POSTGRES_PORT=15439", "POSTGRES_USER=tr_s01", "POSTGRES_DB="+database, "POSTGRES_PASSWORD=fixture-only", "REDIS_PASSWORD=fixture-only", "TDENGINE_ROOT_PASSWORD=fixture-only", "JWT_SECRET="+strings.Repeat("t", 32), "SIMULATION_ALARM_MODE=off", "AGENT_ENABLED=false", "FEISHU_BUSINESS_ENABLED=false")
		output, e := cmd.CombinedOutput()
		if e != nil {
			t.Fatalf("actual API migration %s: %v %s", database, e, output)
		}
	}
	runMigration("tr_s01_test")
	runMigration("tr_s01_test")
	mustTR(t, db.Exec("CREATE DATABASE tr_s01_fresh_test").Error)
	runMigration("tr_s01_fresh_test")
	runMigration("tr_s01_fresh_test")
	t.Log("actual API entry: fresh install, repeat and legacy upgrade preserved old NULL state")
	var legacy model.Inbox
	mustTR(t, db.Where("run_id=?", sample.RunID).First(&legacy).Error)
	if legacy.BusinessState != nil || legacy.ProcessedAt == nil {
		t.Fatal("legacy rewritten")
	}
	var historic struct {
		KWh           float64 `gorm:"column:kwh"`
		SourceQuality string
	}
	mustTR(t, db.Raw("SELECT kwh,source_quality FROM energy_daily WHERE device_id=? AND day='2000-01-01'", d.ID).Scan(&historic).Error)
	if historic.KWh != 13.7 || historic.SourceQuality != "legacy_unknown" {
		t.Fatalf("historic aggregate: %+v", historic)
	}
	var indexCount int64
	mustTR(t, db.Raw("SELECT count(*) FROM pg_indexes WHERE tablename='telemetry_inbox' AND indexname IN ('idx_inbox_pending','idx_inbox_pending_device')").Scan(&indexCount).Error)
	if indexCount != 2 {
		t.Fatal("legacy pending indexes lost")
	}
	planTx := db.Begin()
	mustTR(t, planTx.Exec("SET LOCAL enable_seqscan=off").Error)
	var plan []string
	mustTR(t, planTx.Raw("EXPLAIN (COSTS OFF) SELECT id FROM telemetry_inbox WHERE processed_at IS NULL ORDER BY id LIMIT 1").Scan(&plan).Error)
	mustTR(t, planTx.Rollback().Error)
	if !strings.Contains(strings.Join(plan, " "), "idx_inbox_pending") {
		t.Fatalf("pending index unused: %v", plan)
	}
	t.Logf("queue indexed plan: %v", plan)
	var mode atomic.Int32
	entered := make(chan struct{}, 1)
	release := make(chan struct{})
	td := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch mode.Load() {
		case 1:
			w.WriteHeader(503)
			return
		case 2:
			select {
			case entered <- struct{}{}:
			default:
			}
			select {
			case <-release:
			case <-r.Context().Done():
				return
			}
		case 3:
			json.NewEncoder(w).Encode(map[string]any{"code": 0, "data": [][]any{}})
			return
		}
		json.NewEncoder(w).Encode(map[string]any{"code": 0, "data": [][]any{{1}}})
	}))
	defer td.Close()
	cfg := config.Config{TDURL: td.URL, TDDatabase: "tr_s01", JWTSecret: strings.Repeat("t", 32)}
	s := New(cfg, db, rd, tdengine.New(cfg), zap.NewNop())
	s.fleetCatalog, _ = fleet.Load("../../../01-data-chain/simulator/fleet.json")
	read := func(key string) float64 {
		s.telemetry.mu.Lock()
		defer s.telemetry.mu.Unlock()
		return s.telemetry.values[key]
	}
	row := func(seq int64, received time.Time) model.Inbox {
		sample.Seq = seq
		sample.TS = time.Now().UnixMilli()
		b, _ := json.Marshal(sample)
		item := model.Inbox{RunID: sample.RunID, DeviceID: d.ID, Seq: seq, TS: sample.TS, Payload: b, ReceivedAt: received}
		mustTR(t, db.Create(&item).Error)
		return item
	}
	reload := func(item model.Inbox) model.Inbox {
		var out model.Inbox
		mustTR(t, db.First(&out, item.ID).Error)
		return out
	}
	key := fmt.Sprintf("device:%d:latest", d.ID)
	t.Run("T01_T03_stream_and_coverage", func(t *testing.T) {
		// Deleted records and the manifest's 143 devices cannot set the expected count.
		mustTR(t, db.Exec("UPDATE devices SET deleted_at=now() WHERE id<>?", d.ID).Error)
		d2 := model.Device{DeviceCode: "TR-B", Name: "TR-B", DevType: "inverter", StationCode: "TR"}
		mustTR(t, db.Create(&d2).Error)
		s.probeTelemetry(ctx)
		if read("expected_devices") != 2 || read("missing_keys") != 2 || read("fresh_ratio") != 0 {
			t.Fatalf("missing coverage %v", s.telemetry.values)
		}
		sample.TS = time.Now().UnixMilli()
		_, e := s.cacheLatest(ctx, d, sample, time.Now())
		mustTR(t, e)
		s.probeTelemetry(ctx)
		if read("fresh_ratio") != 0.5 || read("missing_keys") != 1 {
			t.Fatal("one device masked another")
		}
		_, e = s.cacheLatest(ctx, d2, sample, time.Now().Add(-time.Minute))
		mustTR(t, e)
		s.probeTelemetry(ctx)
		if read("cache_received_worst_age_seconds") < 59 || read("fresh_ratio") != 0.5 {
			t.Fatal("stale content hidden by recent SET")
		}
		mustTR(t, db.Exec("UPDATE telemetry_inbox SET received_at=?", time.Now().Add(-2*time.Minute)).Error)
		s.probeTelemetry(ctx)
		if read("inbox_receive_age_seconds") < 119 {
			t.Fatalf("stopped input age=%v", read("inbox_receive_age_seconds"))
		}
		mustTR(t, db.Exec("UPDATE devices SET deleted_at=now() WHERE id=?", d2.ID).Error)
	})
	t.Run("T02_T06_retry_quarantine_and_forward_recovery", func(t *testing.T) {
		item := row(2, time.Now().Add(-24*time.Hour))
		if item.BusinessState == nil || *item.BusinessState != "pending" {
			t.Fatal("new row must use pending default")
		}
		mode.Store(1)
		s.processPending(ctx)
		item = reload(item)
		if item.BusinessState == nil || *item.BusinessState != "retry" || item.ProcessedAt != nil || item.FirstFailedAt == nil || time.Since(*item.FirstFailedAt) > 5*time.Second {
			t.Fatal("old receive time became terminal/failure start")
		}
		if db.Exec("UPDATE telemetry_inbox SET processed_at=now() WHERE id=?", item.ID).Error == nil {
			t.Fatal("unsafe old writer accepted")
		}
		mode.Store(0)
		mustTR(t, db.Exec("UPDATE telemetry_inbox SET next_attempt_at=now()-interval '1 hour' WHERE id=?", item.ID).Error)
		// Make business commit fail after Redis succeeds.
		mustTR(t, db.Exec("ALTER TABLE energy_dirty_days RENAME TO tr_saved_dirty").Error)
		s.processPending(ctx)
		item = reload(item)
		if *item.BusinessState != "retry" || item.TDConfirmedAt == nil || item.ProcessedAt != nil {
			t.Fatalf("business failure state: %+v", item)
		}
		n, e := rd.Exists(ctx, key).Result()
		mustTR(t, e)
		if n != 1 {
			t.Fatal("cache write not observed before business failure")
		}
		s.telemetry.mu.Lock()
		successBefore := s.telemetry.times["success"]
		s.telemetry.mu.Unlock()
		if !successBefore.IsZero() {
			t.Fatal("failed transaction refreshed success")
		}
		mustTR(t, db.Exec("ALTER TABLE tr_saved_dirty RENAME TO energy_dirty_days").Error)
		mustTR(t, db.Exec("UPDATE telemetry_inbox SET next_attempt_at=now()-interval '1 hour' WHERE id=?", item.ID).Error)
		s.processPending(ctx)
		item = reload(item)
		if *item.BusinessState != "success" || item.ProcessedAt == nil {
			t.Fatal("current worker did not consume retry")
		}
		bad := row(3, time.Now())
		mustTR(t, db.Exec("UPDATE telemetry_inbox SET payload='{}'::jsonb WHERE id=?", bad.ID).Error)
		s.processPending(ctx)
		bad = reload(bad)
		if *bad.BusinessState != "quarantined" || *bad.FailureCode != "payload_invalid" {
			t.Fatal("bad row was not isolated")
		}
		s.telemetry.mu.Lock()
		if s.telemetry.times["success"].IsZero() || s.telemetry.failures["payload_invalid"] != 1 {
			t.Fatal("state metrics absent")
		}
		s.telemetry.mu.Unlock()
		t.Log("old completion writer rejected; forward candidate consumed retained retry and committed business+dirty")
	})
	t.Run("T04_independent_probe_and_failures", func(t *testing.T) {
		row(4, time.Now())
		mode.Store(2)
		done := make(chan struct{})
		go func() { s.processPending(ctx); close(done) }()
		select {
		case <-entered:
		case <-time.After(3 * time.Second):
			t.Fatal("worker did not enter TD fixture")
		}
		probeCtx, cancel := context.WithCancel(ctx)
		go s.telemetryWatchdog(probeCtx)
		time.Sleep(5500 * time.Millisecond)
		s.telemetry.mu.Lock()
		at := s.telemetry.times["probe"]
		s.telemetry.mu.Unlock()
		if time.Since(at) > 2*time.Second {
			t.Fatal("worker blocked watchdog")
		}
		cancel()
		close(release)
		<-done
		mode.Store(0)
		// Dependency errors export unknown values; metrics stay available without I/O.
		saved := s.redis
		s.redis = redis.NewClient(&redis.Options{Addr: "127.0.0.1:1", MaxRetries: -1, DialTimeout: 50 * time.Millisecond})
		s.probeTelemetry(ctx)
		if read("probe_redis_ok") != 0 || !math.IsNaN(read("fresh_ratio")) {
			t.Fatal("failed probe exported healthy coverage")
		}
		s.redis.Close()
		s.redis = saved
		mustTR(t, db.Exec("ALTER TABLE devices RENAME TO tr_saved_devices").Error)
		s.probeTelemetry(ctx)
		if read("probe_pg_ok") != 0 || !math.IsNaN(read("expected_devices")) {
			t.Fatal("failed registry cleared expectation")
		}
		mustTR(t, db.Exec("ALTER TABLE tr_saved_devices RENAME TO devices").Error)
		s.telemetry.touch("probe", time.Now().Add(-time.Minute))
		w := httptest.NewRecorder()
		s.Handler().ServeHTTP(w, httptest.NewRequest("GET", "/metrics", nil))
		if w.Code != 200 || !strings.Contains(w.Body.String(), "powersystem_telemetry_probe_age_seconds 60") {
			t.Fatal("stopped probe invisible")
		}
	})
	t.Run("T05_silence_maintenance_and_clock", func(t *testing.T) {
		catalog := s.fleetCatalog
		s.fleetCatalog = nil
		s.probeTelemetry(ctx)
		if read("expected_devices") != 1 || read("classification_known") != 0 {
			t.Fatal("missing fleet changed registration authority")
		}
		s.fleetCatalog = catalog

		mustTR(t, db.Exec("INSERT INTO telemetry_silence_windows(device_id,starts_at,ends_at,reason,created_by) VALUES(?,now()-interval '1 minute',now()+interval '1 minute','fixture planned stop','test-owner')", d.ID).Error)
		s.probeTelemetry(ctx)
		if read("expected_devices") != 0 || !math.IsNaN(read("fresh_ratio")) {
			t.Fatal("intentional idle became full coverage")
		}
		mustTR(t, db.Exec("UPDATE telemetry_silence_windows SET ends_at=now()-interval '1 second'").Error)
		mustTR(t, db.Exec("UPDATE alarm_maintenance_state SET enabled=true,updated_at=now()-interval '31 minutes' WHERE id=1").Error)
		s.probeTelemetry(ctx)
		if read("maintenance") != 1 || read("maintenance_age_seconds") < 1800 {
			t.Fatal("maintenance overrun hidden")
		}
		mustTR(t, db.Exec("UPDATE alarm_maintenance_state SET enabled=false WHERE id=1").Error)
		mustTR(t, db.Exec("DELETE FROM alarm_maintenance_state WHERE id=1").Error)
		s.probeTelemetry(ctx)
		if read("probe_pg_ok") != 1 || read("maintenance_known") != 0 || !math.IsNaN(read("maintenance")) {
			t.Fatal("missing singleton healthy")
		}
		mustTR(t, db.Exec("INSERT INTO alarm_maintenance_state(id) VALUES(1)").Error)
		if !math.IsNaN(telemetryAge(nil, time.Now())) {
			t.Fatal("uninitialized age healthy")
		}
	})
	t.Run("T07_restore_energy_and_readers", func(t *testing.T) {
		mode.Store(0)
		mustTR(t, rd.Del(ctx, key).Err())
		s.restoreLatest(ctx)
		successRaw, e := rd.Get(ctx, key).Bytes()
		mustTR(t, e)
		var successSnapshot struct {
			Seq int64 `json:"seq"`
		}
		mustTR(t, json.Unmarshal(successRaw, &successSnapshot))
		if successSnapshot.Seq != 4 {
			t.Fatal("business success restore lost latest verified candidate")
		}

		mode.Store(3)
		mustTR(t, rd.Del(ctx, key).Err())
		// Exclude new successes temporarily so only ambiguous legacy candidates remain.
		mustTR(t, db.Exec("UPDATE telemetry_inbox SET business_state='quarantined' WHERE business_state='success'").Error)
		s.restoreLatest(ctx)
		n, e := rd.Exists(ctx, key).Result()
		mustTR(t, e)
		if n != 0 {
			t.Fatal("unverified legacy restored")
		}
		mode.Store(0)
		s.restoreLatest(ctx)
		n, e = rd.Exists(ctx, key).Result()
		mustTR(t, e)
		if n != 1 {
			t.Fatal("verified legacy not restored")
		}
		var restored struct {
			Seq int64 `json:"seq"`
		}
		raw, e := rd.Get(ctx, key).Bytes()
		mustTR(t, e)
		mustTR(t, json.Unmarshal(raw, &restored))
		if restored.Seq != 1 {
			t.Fatal("quarantined snapshot restored")
		}

		// Dirty rebuild keeps legacy energy numeric, but reports provenance as provisional.
		mustTR(t, energy.MarkDirty(db, d.ID, legacy.TS))
		mustTR(t, store.Migrate(db, "../../deploy/postgres/backfill_energy.sql"))
		_, err := energy.RebuildPending(ctx, db, 12)
		mustTR(t, err)
		var rebuiltKWh float64
		mustTR(t, db.Raw("SELECT kwh FROM energy_daily WHERE device_id=? AND day=?", d.ID, time.Now().In(energyReportZone).Format("2006-01-02")).Scan(&rebuiltKWh).Error)
		if rebuiltKWh != 0 {
			t.Fatal("quarantined samples leaked into energy integration")
		}
		var quality string
		mustTR(t, db.Raw("SELECT source_quality FROM energy_daily WHERE device_id=? AND day=?", d.ID, time.Now().In(energyReportZone).Format("2006-01-02")).Scan(&quality).Error)
		if quality != "provisional" {
			t.Fatal("legacy energy claimed verified")
		}
		now := time.Now()
		days := []string{now.In(energyReportZone).Format("2006-01-02")}
		rows, e := s.energyReportRows([]model.Device{d}, days[0], days[0])
		mustTR(t, e)
		// A numeric old aggregate remains readable while completeness is false.
		mustTR(t, db.Exec("UPDATE energy_daily SET kwh=12.5,segments_count=1 WHERE device_id=?", d.ID).Error)
		rows, e = s.energyReportRows([]model.Device{d}, days[0], days[0])
		mustTR(t, e)
		report := buildEnergyReport(scopeSelection{Scope: "all"}, 0, []model.Device{d}, rows, days, now.Add(24*time.Hour), now)
		if report.Complete || report.TotalKWh == nil || *report.TotalKWh != 12.5 || len(report.Missing) == 0 || report.Missing[0].Reason != "source_uncertain" {
			t.Fatal("legacy report completeness/numeric value lost")
		}
		mustTR(t, db.Exec("UPDATE energy_daily SET source_quality='verified' WHERE device_id=? AND day=?", d.ID, days[0]).Error)
		summary, e := energy.ReadSummary(ctx, db, now)
		mustTR(t, e)
		if summary.TodayProvisional || !summary.RetainedProvisional {
			t.Fatal("historic uncertainty contaminated today's provenance")
		}
		overview, e := s.overviewEnergy([]model.Device{d}, now)
		mustTR(t, e)
		if overview[d.ID].Provisional {
			t.Fatal("today overview scope wrong")
		}

	})
}
func mustTR(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatal(err)
	}
}
