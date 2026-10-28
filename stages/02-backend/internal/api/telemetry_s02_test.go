//go:build tr_s02

package api

import (
	"context"
	"database/sql"
	"database/sql/driver"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/eclipse/paho.mqtt.golang/packets"
	"github.com/gin-gonic/gin"
	"github.com/gorilla/websocket"
	"github.com/jackc/pgx/v5/stdlib"
	"github.com/redis/go-redis/v9"
	"go.uber.org/zap"
	"go.uber.org/zap/zaptest/observer"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"powersystem/backend/internal/config"
	"powersystem/backend/internal/energy"
	"powersystem/backend/internal/fleet"
	"powersystem/backend/internal/model"

	"powersystem/backend/internal/store"
	"powersystem/backend/internal/tdengine"
	"powersystem/backend/internal/telemetry"
)

func s02must(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatal(err)
	}
}
func s02wait(t *testing.T, fn func() bool) {
	t.Helper()
	until := time.Now().Add(3 * time.Second)
	for !fn() {
		if time.Now().After(until) {
			t.Fatal("bounded wait expired")
		}
		time.Sleep(10 * time.Millisecond)
	}
}
func TestTRS02Isolated(t *testing.T) {
	dsn := os.Getenv("TR_S02_TEST_DSN")
	if isolated := os.Getenv("TR_S03_TEST_DSN"); isolated != "" {
		dsn = isolated
	}
	if dsn != "postgres://tr_s02@127.0.0.1:15440/tr_s02_test?sslmode=disable" && dsn != "postgres://tr_s03@127.0.0.1:15441/tr_s03_test?sslmode=disable" {
		t.Fatal("dedicated S02 fixture required; no SKIP")
	}
	db, err := gorm.Open(postgres.Open(dsn), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	s02must(t, err)
	pool, _ := db.DB()
	defer pool.Close()
	s02must(t, db.Exec("DROP SCHEMA public CASCADE; CREATE SCHEMA public").Error)
	for _, path := range store.MigrationPaths {
		s02must(t, store.Migrate(db, filepath.Join("../..", path)))
	}
	redisAddress := "127.0.0.1:16390"
	if os.Getenv("TR_S03_TEST_DSN") != "" {
		redisAddress = "127.0.0.1:16391"
	}
	rd := redis.NewClient(&redis.Options{Addr: redisAddress, ContextTimeoutEnabled: true, MaxRetries: -1})
	defer rd.Close()
	s02must(t, rd.Ping(context.Background()).Err())
	s02must(t, rd.FlushDB(context.Background()).Err())
	var mode atomic.Int32
	td := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if mode.Load() == 1 {
			select {
			case <-r.Context().Done():
				return
			case <-time.After(5 * time.Second):
			}
		}
		json.NewEncoder(w).Encode(map[string]any{"code": 0, "data": [][]any{{1}}})
	}))
	defer td.Close()
	core, logs := observer.New(zap.WarnLevel)
	cfg := config.Config{TDURL: td.URL, TDDatabase: "tr_s02", JWTSecret: strings.Repeat("x", 32), SimulationAlarmMode: "off"}
	s := New(cfg, db, rd, tdengine.New(cfg), zap.New(core))
	s.fleetCatalog, err = fleet.Load("../../../01-data-chain/simulator/fleet.json")
	s02must(t, err)
	defer func() {
		if s.realtime.cancel != nil {
			s.realtime.cancel()
			<-s.realtime.done
		}
	}()
	var seq int64
	makeItem := func() (model.Device, model.Inbox) {
		seq++
		d := model.Device{DeviceCode: fmt.Sprintf("S02-%d", seq), Name: "fixture", DevType: "inverter", StationCode: "S02"}
		s02must(t, db.Create(&d).Error)
		sample := telemetry.Sample{SchemaVersion: 1, RunID: "s02", DeviceCode: d.DeviceCode, StationCode: d.StationCode, Seq: seq, TS: time.Now().Add(-time.Second).UnixMilli(), Temperature: 60, Voltage: 400, Current: 100, Power: 68.589211, Status: 1}
		payload, _ := json.Marshal(sample)
		s02must(t, db.Exec("INSERT INTO telemetry_inbox(run_id,device_id,seq,ts_ms,payload,received_at) VALUES(?,?,?,?,?::jsonb,?)", sample.RunID, d.ID, sample.Seq, sample.TS, string(payload), time.Now().Add(-time.Second)).Error)
		var item model.Inbox
		s02must(t, db.Where("device_id=?", d.ID).First(&item).Error)
		s02must(t, db.Create(&model.AlarmRule{DeviceID: d.ID, Metric: "temperature", Operator: ">", Threshold: 50, Level: "major", Enabled: true, Version: "1"}).Error)
		return d, item
	}
	count := func(table string, id int64) int64 {
		var n int64
		s02must(t, db.Table(table).Where("device_id=?", id).Count(&n).Error)
		return n
	}
	terminal := func(item model.Inbox) bool {
		var row model.Inbox
		s02must(t, db.First(&row, item.ID).Error)
		return row.ProcessedAt != nil && row.BusinessState != nil && *row.BusinessState == "success"
	}
	t.Run("T01_four_branches_and_GET", func(t *testing.T) {
		for _, tc := range []struct {
			sql, state string
			stop       bool
		}{
			{"UPDATE alarm_maintenance_state SET enabled=false WHERE id=1", "disabled", false},
			{"UPDATE alarm_maintenance_state SET enabled=true WHERE id=1", "enabled", true},
			{"DELETE FROM alarm_maintenance_state WHERE id=1", "missing", true},
			{"ALTER TABLE alarm_maintenance_state RENAME TO s02_gate_hidden", "sql_error", true},
		} {
			s02must(t, db.Exec(tc.sql).Error)
			if got := s.maintenanceState(context.Background()); got != tc.state {
				t.Fatalf("state %s != %s", got, tc.state)
			}
			if s.alarmMaintenance(context.Background()) != tc.stop {
				t.Fatal("gate branch")
			}
			s.probeTelemetry(context.Background())
			s.telemetry.mu.Lock()
			missing, sqlerror := s.telemetry.values["maintenance_missing"], s.telemetry.values["maintenance_sql_error"]
			s.telemetry.mu.Unlock()
			if tc.state == "missing" && missing != 1 {
				t.Fatal("missing metric")
			}
			if tc.state == "sql_error" && sqlerror != 1 {
				t.Fatal("SQL metric")
			}
		}
		s02must(t, db.Exec("ALTER TABLE s02_gate_hidden RENAME TO alarm_maintenance_state; INSERT INTO alarm_maintenance_state(id,enabled)VALUES(1,true)").Error)
		// Actual GET business handlers under a database-level read-only session.
		u := model.User{Username: "s02", PasswordHash: "fixture", Role: "admin"}
		s02must(t, db.Create(&u).Error)
		token, e := s.sign(u)
		s02must(t, e)
		ro, err := gorm.Open(postgres.Open(dsn+"&default_transaction_read_only=on"), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
		s02must(t, err)
		rp, _ := ro.DB()
		defer rp.Close()
		rs := New(cfg, ro, rd, s.td, zap.NewNop())
		for _, url := range []string{"/api/v1/devices", "/api/v1/alarms", "/api/v1/alarm-rules", "/api/v1/reports/energy?start_date=2026-10-26&end_date=2026-10-27", "/api/v1/agent/interpretations"} {
			w := httptest.NewRecorder()
			r := httptest.NewRequest("GET", url, nil)
			r.Header.Set("Authorization", "Bearer "+token)
			rs.Handler().ServeHTTP(w, r)
			if w.Code != 200 {
				t.Fatalf("read-only GET %s => %d %s", url, w.Code, w.Body.String())
			}
		}
		s02must(t, db.Exec("UPDATE alarm_maintenance_state SET enabled=false WHERE id=1").Error)
		t.Log("four distinct states, watchdog metrics, read-only PG GET handlers verified; WS ticket GETDEL is explicitly excluded")
	})
	t.Run("T02_freeze_drains_inflight", func(t *testing.T) {
		d, item := makeItem()
		lock := db.Begin()
		defer lock.Rollback()
		s02must(t, lock.Exec("SELECT id FROM alarm_rules WHERE device_id=? FOR UPDATE", d.ID).Error)
		done := make(chan struct{})
		go func() { s.processInboxItem(context.Background(), item); close(done) }()
		s02wait(t, func() bool {
			var n int64
			db.Raw("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock' AND query LIKE '%alarm_rules%'").Scan(&n)
			return n > 0
		})
		// The independent watchdog remains able to probe while business is blocked.
		s.probeTelemetry(context.Background())
		s.telemetry.mu.Lock()
		probeOK := s.telemetry.values["probe_pg_ok"]
		s.telemetry.mu.Unlock()
		if probeOK != 1 {
			t.Fatal("watchdog stalled behind business")
		}
		freeze := make(chan error, 1)
		go func() {
			freeze <- db.Exec("UPDATE alarm_maintenance_state SET enabled=true,updated_at=now() WHERE id=1").Error
		}()
		select {
		case e := <-freeze:
			t.Fatalf("freeze crossed active transaction: %v", e)
		case <-time.After(100 * time.Millisecond):
		}
		s02must(t, lock.Rollback().Error)
		<-done
		s02must(t, <-freeze)
		if !terminal(item) {
			t.Fatal("inflight transaction did not commit before barrier")
		}
		d2, next := makeItem()
		s.processInboxItem(context.Background(), next)
		if terminal(next) || count("alarm_records", d2.ID) != 0 {
			t.Fatal("write beyond freeze boundary")
		}
		if count("alarm_records", d.ID) != 1 {
			t.Fatal("expected one drained alarm")
		}
		s02must(t, db.Exec("UPDATE alarm_maintenance_state SET enabled=false WHERE id=1").Error)
		s.processInboxItem(context.Background(), next)
		if !terminal(next) {
			t.Fatal("explicit resume failed")
		}
		// The actual HTTP ack must participate in the same freeze barrier.
		var alarm model.AlarmRecord
		s02must(t, db.Where("device_id=?", d2.ID).First(&alarm).Error)
		var user model.User
		s02must(t, db.First(&user).Error)
		token, e := s.sign(user)
		s02must(t, e)
		held := db.Begin()
		defer held.Rollback()
		s02must(t, held.Exec("SELECT id FROM alarm_records WHERE id=? FOR UPDATE", alarm.ID).Error)
		httpDone := make(chan int, 1)
		go func() {
			w := httptest.NewRecorder()
			r := httptest.NewRequest("POST", fmt.Sprintf("/api/v1/alarms/%d/ack", alarm.ID), nil)
			r.Header.Set("Authorization", "Bearer "+token)
			s.Handler().ServeHTTP(w, r)
			httpDone <- w.Code
		}()
		s02wait(t, func() bool {
			var n int64
			db.Raw("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock' AND query LIKE '%alarm_records%'").Scan(&n)
			return n > 0
		})
		go func() { freeze <- db.Exec("UPDATE alarm_maintenance_state SET enabled=true WHERE id=1").Error }()
		select {
		case e := <-freeze:
			t.Fatalf("HTTP freeze did not drain: %v", e)
		case <-time.After(80 * time.Millisecond):
		}
		held.Rollback()
		if code := <-httpDone; code != 200 {
			t.Fatalf("HTTP ack=%d", code)
		}
		s02must(t, <-freeze)
		if _, e := energy.RebuildPending(context.Background(), db, 1); e == nil {
			t.Fatal("energy wrote past maintenance")
		}
		w := httptest.NewRecorder()
		r := httptest.NewRequest("POST", fmt.Sprintf("/api/v1/alarms/%d/archive", alarm.ID), nil)
		r.Header.Set("Authorization", "Bearer "+token)
		s.Handler().ServeHTTP(w, r)
		if w.Code != 503 {
			t.Fatal("HTTP bypassed maintenance")
		}
		// Outbox dispatcher is gated too; no external network is involved.
		var deliveries atomic.Int32
		receiver := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { deliveries.Add(1); w.WriteHeader(200) }))
		defer receiver.Close()
		oldURL := s.cfg.BusinessNotifyURL
		s.cfg.BusinessNotifyURL = receiver.URL
		s02must(t, s.deliverBusinessNotification(context.Background()))
		if deliveries.Load() != 0 {
			t.Fatal("maintenance delivered a card")
		}
		s.cfg.BusinessNotifyURL = oldURL
		s02must(t, db.Exec("UPDATE alarm_maintenance_state SET enabled=false WHERE id=1").Error)
		t.Log("UPDATE drains telemetry and actual HTTP ack; no post-barrier alarm/outbox/inbox/energy or local fake notification")
	})
	t.Run("T03_queue_lock_timeout_watchdog_recovery", func(t *testing.T) {
		_, item := makeItem()
		lock := db.Begin()
		defer lock.Rollback()
		s02must(t, lock.Exec("LOCK TABLE telemetry_inbox IN ACCESS EXCLUSIVE MODE").Error)
		ctx, cancel := context.WithTimeout(context.Background(), 180*time.Millisecond)
		start := time.Now()
		s.processPending(ctx)
		cancel()
		if time.Since(start) > time.Second {
			t.Fatal("queue SQL exceeded parent deadline")
		}
		lock.Rollback()
		s.probeTelemetry(context.Background())
		s.processInboxItem(context.Background(), item)
		if !terminal(item) {
			t.Fatal("next round failed")
		}
		t.Logf("queue lock cancellation=%s; independent probe and subsequent transaction recovered", time.Since(start))
	})
	t.Run("T04_transaction_cancellation", func(t *testing.T) {
		for _, stage := range []string{"inbox_row", "rules_query", "rules_row", "alarm_records", "business_notification_outbox", "energy_dirty_days"} {
			t.Run(stage, func(t *testing.T) {
				d, item := makeItem()
				var lock *gorm.DB
				if stage == "inbox_row" || stage == "rules_row" || stage == "rules_query" {
					lock = db.Begin()
					switch stage {
					case "inbox_row":
						s02must(t, lock.Exec("SELECT id FROM telemetry_inbox WHERE id=? FOR UPDATE", item.ID).Error)
					case "rules_row":
						s02must(t, lock.Exec("SELECT id FROM alarm_rules WHERE device_id=? FOR UPDATE", d.ID).Error)
					case "rules_query":
						s02must(t, lock.Exec("LOCK TABLE alarm_rules IN ACCESS EXCLUSIVE MODE").Error)
					}
				} else {
					s02must(t, db.Exec("CREATE OR REPLACE FUNCTION s02_slow() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_sleep(10); RETURN NEW; END $$").Error)
					s02must(t, db.Exec("CREATE TRIGGER s02_slow BEFORE INSERT ON "+stage+" FOR EACH ROW EXECUTE FUNCTION s02_slow()").Error)
				}
				ctx, cancel := context.WithTimeout(context.Background(), 180*time.Millisecond)
				start := time.Now()
				s.processInboxItem(ctx, item)
				cancel()
				if time.Since(start) > time.Second {
					t.Fatal("cancel exceeded bound")
				}
				if lock != nil {
					lock.Rollback()
				} else {
					s02must(t, db.Exec("DROP TRIGGER s02_slow ON "+stage).Error)
				}
				if terminal(item) || count("alarm_records", d.ID) != 0 || count("energy_dirty_days", d.ID) != 0 {
					t.Fatal("partial transaction committed")
				}
				var out int64
				s02must(t, db.Raw("SELECT count(*) FROM business_notification_outbox WHERE payload->>'device_code'=?", d.DeviceCode).Scan(&out).Error)
				if out != 0 {
					t.Fatal("outbox escaped rollback")
				}
				s.processInboxItem(context.Background(), item)
				if !terminal(item) || count("alarm_records", d.ID) != 1 {
					t.Fatal("recovery failed")
				}
				t.Logf("rollback and recovery at %s verified", stage)
			})
		}
	})
	t.Run("T05_slow_TD_Redis", func(t *testing.T) {
		d, item := makeItem()
		mode.Store(1)
		start := time.Now()
		s.processInboxItem(context.Background(), item)
		mode.Store(0)
		if time.Since(start) > 2*time.Second || terminal(item) {
			t.Fatal("TD bound/success")
		}
		var row model.Inbox
		db.First(&row, item.ID)
		if row.FailureCode == nil || *row.FailureCode != "td_unavailable" {
			t.Fatalf("TD reason %+v", row.FailureCode)
		}
		// S03 enforces the durable deadline even for direct calls. Make the
		// dedicated test retry due explicitly; the backoff itself is tested in S03.
		s02must(t, db.Exec("UPDATE telemetry_inbox SET next_attempt_at=? WHERE id=?", time.Now().Add(-time.Second), item.ID).Error)
		s.processInboxItem(context.Background(), item)
		if !terminal(item) {
			t.Fatal("TD recovery")
		}
		listener, e := net.Listen("tcp", "127.0.0.1:0")
		s02must(t, e)
		defer listener.Close()
		stop := make(chan struct{})
		defer close(stop)
		go func() {
			for {
				conn, e := listener.Accept()
				if e != nil {
					return
				}
				go func() { defer conn.Close(); <-stop }()
			}
		}()
		slow := redis.NewClient(&redis.Options{Addr: listener.Addr().String(), ContextTimeoutEnabled: true, MaxRetries: -1})
		defer slow.Close()
		s.redis = slow
		_, item2 := makeItem()
		start = time.Now()
		s.processInboxItem(context.Background(), item2)
		s.redis = rd
		if time.Since(start) > 2*time.Second || terminal(item2) {
			t.Fatal("Redis bound/success")
		}
		db.First(&row, item2.ID)
		s02must(t, db.Exec("UPDATE telemetry_inbox SET next_attempt_at=? WHERE id=?", time.Now().Add(-time.Second), item2.ID).Error)
		s.processInboxItem(context.Background(), item2)
		if !terminal(item2) {
			t.Fatal("Redis recovery")
		}
		if count("alarm_records", d.ID) != 1 {
			t.Fatal("duplicate after dependency recovery")
		}
		t.Log("TD REST and blackhole Redis each bounded to one second; retry not quarantine, no stale-age discard")
	})
	t.Run("T06_slow_websockets_and_refresh", func(t *testing.T) { s02Websockets(t, s, rd, makeItem, terminal) })
	t.Run("T07_restore_and_readiness", func(t *testing.T) {
		// A dedicated modest history fixture validates the actual partial index,
		// without turning this into S04 capacity or replay testing.
		var first model.Device
		s02must(t, db.First(&first).Error)
		s02must(t, db.Exec(`INSERT INTO telemetry_inbox(run_id,device_id,seq,ts_ms,payload,received_at,processed_at,business_state,failure_code)
   SELECT 's02-history',?,n,946684800000,'{}'::jsonb,'2000-01-01'::timestamptz,now(),'quarantined','payload_invalid' FROM generate_series(1,12000)n`, first.ID).Error)
		planTx := db.Begin()
		defer planTx.Rollback()
		s02must(t, planTx.Exec("SET LOCAL enable_seqscan=off").Error)
		var plan []string
		s02must(t, planTx.Raw(`EXPLAIN (COSTS OFF) SELECT * FROM telemetry_inbox WHERE device_id=? AND (business_state='success' OR (business_state IS NULL AND processed_at IS NOT NULL)) ORDER BY id DESC LIMIT 3`, first.ID).Scan(&plan).Error)
		planTx.Rollback()
		if !strings.Contains(strings.Join(plan, " "), "idx_inbox_restore") {
			t.Fatalf("restore index: %v", plan)
		}
		t.Logf("12000-row dedicated history fixture; bounded restore plan %v", plan)
		s.setStartup("restoring", false)
		check := func(want int) {
			w := httptest.NewRecorder()
			s.Handler().ServeHTTP(w, httptest.NewRequest("GET", "/ready", nil))
			if w.Code != want {
				t.Fatalf("ready %d != %d: %s values=%+v", w.Code, want, w.Body.String(), s.telemetry.values)
			}
		}
		check(503)
		lock := db.Begin()
		defer lock.Rollback()
		s02must(t, lock.Exec("LOCK TABLE telemetry_inbox IN ACCESS EXCLUSIVE MODE").Error)
		ctx, cancel := context.WithTimeout(context.Background(), 180*time.Millisecond)
		start := time.Now()
		e := s.restoreLatest(ctx)
		cancel()
		lock.Rollback()
		if e == nil || time.Since(start) > time.Second {
			t.Fatal("restore lock deadline")
		}
		check(503)
		s02must(t, s.restoreLatest(context.Background()))
		s.setStartup("running", true)
		s.probeTelemetry(context.Background())
		check(200)
		s.redis = redis.NewClient(&redis.Options{Addr: "127.0.0.1:1", DialTimeout: 20 * time.Millisecond, MaxRetries: -1, ContextTimeoutEnabled: true})
		e = s.restoreLatest(context.Background())
		s.probeTelemetry(context.Background())
		check(503)
		s.redis.Close()
		s.redis = rd
		if e == nil {
			t.Fatal("restore dependency error hidden")
		}
		s02must(t, s.restoreLatest(context.Background()))
		s.probeTelemetry(context.Background())
		check(200)
		mode.Store(1)
		s.probeTelemetry(context.Background())
		check(503)
		if s.telemetry.values["probe_td_state"] != 3 {
			t.Fatal("TD timeout diagnostic")
		}
		mode.Store(0)
		s.probeTelemetry(context.Background())
		check(200)
		s.telemetry.mu.Lock()
		s.telemetry.times["probe"] = time.Now().Add(-14 * time.Second)
		s.telemetry.mu.Unlock()
		check(503)
		s.probeTelemetry(context.Background())
		check(200)
		t.Log("bounded indexed restore, dependency errors and actual freshness readiness verified")
	})
	t.Run("T08_unknown_commit_shutdown_restart_logs", func(t *testing.T) {
		sql.Register("s02-unknown", &s02Driver{})
		wrapped, e := sql.Open("s02-unknown", dsn)
		s02must(t, e)
		defer wrapped.Close()
		wdb, e := gorm.Open(postgres.New(postgres.Config{Conn: wrapped}), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
		s02must(t, e)
		old := s.db
		s.db = wdb
		d, item := makeItem()
		unknownMode.Store(1)
		s.processInboxItem(context.Background(), item)
		s.db = old
		if !terminal(item) {
			t.Fatal("actual commit not persisted")
		}
		s.processInboxItem(context.Background(), item)
		if count("alarm_records", d.ID) != 1 {
			t.Fatal("indeterminate commit replayed")
		}
		var n int64
		db.Raw("SELECT count(*) FROM business_notification_outbox WHERE payload->>'device_code'=?", d.DeviceCode).Scan(&n)
		if n != 1 {
			t.Fatal("duplicate outbox")
		}
		s.telemetry.mu.Lock()
		unknown := s.telemetry.counts["commit_unknown"]
		s.telemetry.mu.Unlock()
		if unknown != 1 {
			t.Fatalf("unknown metric %d", unknown)
		}
		d2, item2 := makeItem()
		s.db = wdb
		unknownMode.Store(2)
		s.processInboxItem(context.Background(), item2)
		s.db = old
		if terminal(item2) {
			t.Fatal("rollback unexpectedly committed")
		}
		s.processInboxItem(context.Background(), item2)
		if !terminal(item2) || count("alarm_records", d2.ID) != 1 {
			t.Fatal("resolve rollback failed")
		}
		// Cancel workers during a real lock wait, then restart with no residual lock.
		_, pending := makeItem()
		lock := db.Begin()
		defer lock.Rollback()
		s02must(t, lock.Exec("SELECT id FROM telemetry_inbox WHERE id=? FOR UPDATE", pending.ID).Error)
		ctx, cancel := context.WithCancel(context.Background())
		done := make(chan struct{})
		go func() { s.worker(ctx); close(done) }()
		time.Sleep(600 * time.Millisecond)
		start := time.Now()
		cancel()
		select {
		case <-done:
		case <-time.After(shutdownBudget):
			t.Fatal("worker shutdown")
		}
		lock.Rollback()
		s.processInboxItem(context.Background(), pending)
		if !terminal(pending) {
			t.Fatal("restart held residual lock")
		}
		before := logs.Len()
		now := time.Now()
		s.telemetryLog("s02_limit", now)
		for i := 0; i < 30; i++ {
			s.telemetryLog("s02_limit", now.Add(59*time.Second))
		}
		s.telemetryLog("s02_limit", now.Add(time.Minute))
		if logs.Len()-before != 2 {
			t.Fatal("real minute log limit")
		}
		s02Lifecycle(t, db, rd, s.td, cfg)
		t.Logf("unknown committed/rolled-back results resolved by locked read, one outbox; worker cancellation %s, restart and minute log limiter verified", time.Since(start))
	})
}

// Real PostgreSQL COMMIT, with acknowledgement deliberately lost in the test
// driver. Mode 2 rolls back first, proving both possible durable outcomes.
var unknownMode atomic.Int32

type s02Driver struct{}

func (*s02Driver) Open(name string) (driver.Conn, error) {
	c, e := stdlib.GetDefaultDriver().Open(name)
	if e != nil {
		return nil, e
	}
	return &s02Conn{Conn: c}, nil
}

type s02Conn struct{ driver.Conn }

func (c *s02Conn) BeginTx(ctx context.Context, o driver.TxOptions) (driver.Tx, error) {
	tx, e := c.Conn.(driver.ConnBeginTx).BeginTx(ctx, o)
	if e != nil {
		return nil, e
	}
	return &s02Tx{Tx: tx}, nil
}
func (c *s02Conn) ExecContext(ctx context.Context, q string, a []driver.NamedValue) (driver.Result, error) {
	return c.Conn.(driver.ExecerContext).ExecContext(ctx, q, a)
}
func (c *s02Conn) QueryContext(ctx context.Context, q string, a []driver.NamedValue) (driver.Rows, error) {
	return c.Conn.(driver.QueryerContext).QueryContext(ctx, q, a)
}

type s02Tx struct{ driver.Tx }

func (t *s02Tx) Commit() error {
	switch unknownMode.Swap(0) {
	case 1:
		if e := t.Tx.Commit(); e != nil {
			return e
		}
		return errors.New("fixture lost COMMIT ack")
	case 2:
		t.Tx.Rollback()
		return errors.New("fixture lost transaction outcome")
	}
	return t.Tx.Commit()
}

func s02Websockets(t *testing.T, s *Server, rd *redis.Client, makeItem func() (model.Device, model.Inbox), terminal func(model.Inbox) bool) {
	var slowTransport atomic.Bool
	server := httptest.NewUnstartedServer(s.Handler())
	server.Listener = &s02SlowListener{Listener: server.Listener, slow: &slowTransport}
	server.Start()
	defer server.Close()
	open := func(n int) *websocket.Conn {
		ticket := fmt.Sprintf("%048d", n)
		s02must(t, rd.Set(context.Background(), "ws-ticket:"+ticket, "1", time.Minute).Err())
		c, _, e := websocket.DefaultDialer.Dial("ws"+strings.TrimPrefix(server.URL, "http")+"/ws/realtime?ticket="+ticket, nil)
		s02must(t, e)
		return c
	}
	// FIFO for retained events through actual independent socket writer.
	fast := open(1)
	for i := 0; i < 3; i++ {
		s.broadcast(gin.H{"type": "ordered", "n": i})
	}
	for i := 0; i < 3; i++ {
		fast.SetReadDeadline(time.Now().Add(time.Second))
		_, p, e := fast.ReadMessage()
		s02must(t, e)
		var v struct{ N int }
		s02must(t, json.Unmarshal(p, &v))
		if v.N != i {
			t.Fatal("FIFO violated")
		}
	}
	fast.Close()
	slowTransport.Store(true)
	slow := []*websocket.Conn{}
	for i := 2; i < 8; i++ {
		slow = append(slow, open(i))
	}
	defer func() {
		for _, c := range slow {
			c.Close()
		}
	}()
	// Real WebSocket handshakes over loopback; each dedicated transport stalls
	// frame writes until their actual write deadline. OS buffer autotuning cannot
	// make the fault disappear.

	for i := 0; i < 24; i++ {
		s.broadcast(gin.H{"type": "slow", "data": strings.Repeat("x", 12000)})
		time.Sleep(2 * time.Millisecond)
	}
	s02wait(t, func() bool {
		s.telemetry.mu.Lock()
		defer s.telemetry.mu.Unlock()
		return s.telemetry.counts["ws_slow"]+s.telemetry.counts["ws_write_timeout"] > 0
	})
	for i := 20; i < 26; i++ {
		slow = append(slow, open(i))
	}
	// Freeze dispatcher, saturate the real bounded queue, worker remains free.
	s.clientsMu.Lock()
	s.broadcast(gin.H{"type": "hold"})
	time.Sleep(30 * time.Millisecond)
	start := time.Now()
	for i := 0; i < realtimeQueueSize+32; i++ {
		s.broadcast(gin.H{"type": "telemetry", "n": i})
	}
	elapsed := time.Since(start)
	s.clientsMu.Unlock()
	if elapsed > time.Second {
		t.Fatal("broadcast blocked")
	}
	s02wait(t, func() bool {
		s.telemetry.mu.Lock()
		defer s.telemetry.mu.Unlock()
		return s.telemetry.counts["ws_drop"] > 0
	})
	_, item := makeItem()
	s.processInboxItem(context.Background(), item)
	if !terminal(item) {
		t.Fatal("saturated WS stopped business")
	}
	s02wait(t, func() bool { s.clientsMu.Lock(); defer s.clientsMu.Unlock(); return len(s.clients) == 0 })
	slowTransport.Store(false)
	fresh := open(99)
	defer fresh.Close()
	s.broadcast(gin.H{"type": "after_refresh"})
	fresh.SetReadDeadline(time.Now().Add(time.Second))
	_, p, e := fresh.ReadMessage()
	s02must(t, e)
	if !strings.Contains(string(p), "after_refresh") {
		t.Fatal("reconnect order")
	}
	// Actual read-only latest handler recovers current durable view after a gap.
	u := model.User{Username: "ws-s02", PasswordHash: "fixture", Role: "admin"}
	s02must(t, s.db.Create(&u).Error)
	token, e := s.sign(u)
	s02must(t, e)
	req := httptest.NewRequest("GET", fmt.Sprintf("/api/v1/devices/%d/telemetry/latest", item.DeviceID), nil)
	req.Header.Set("Authorization", "Bearer "+token)
	w := httptest.NewRecorder()
	s.Handler().ServeHTTP(w, req)
	if w.Code != 200 {
		t.Fatalf("refresh %d", w.Code)
	}
	t.Logf("six slow sockets, FIFO, bounded global overflow/connection closure, business progress and HTTP refresh verified; enqueue %s", elapsed)
}

func s02Lifecycle(t *testing.T, db *gorm.DB, rd *redis.Client, td *tdengine.Client, cfg config.Config) {
	broker, err := net.Listen("tcp", "127.0.0.1:0")
	s02must(t, err)
	defer broker.Close()
	brokerDone := make(chan struct{})
	go func() {
		defer close(brokerDone)
		conn, e := broker.Accept()
		if e != nil {
			return
		}
		defer conn.Close()
		for {
			packet, e := packets.ReadPacket(conn)
			if e != nil {
				return
			}
			switch p := packet.(type) {
			case *packets.ConnectPacket:
				ack := packets.NewControlPacket(packets.Connack).(*packets.ConnackPacket)
				ack.ReturnCode = 0
				ack.Write(conn)
			case *packets.SubscribePacket:
				ack := packets.NewControlPacket(packets.Suback).(*packets.SubackPacket)
				ack.MessageID = p.MessageID
				ack.ReturnCodes = []byte{1}
				ack.Write(conn)
			case *packets.PingreqPacket:
				packets.NewControlPacket(packets.Pingresp).Write(conn)
			case *packets.DisconnectPacket:
				return
			}
		}
	}()
	listener, e := net.Listen("tcp", "127.0.0.1:0")
	s02must(t, e)
	cfg.HTTPAddr = listener.Addr().String()
	listener.Close()
	cfg.Broker = "tcp://" + broker.Addr().String()
	cfg.ClientID = "tr-s02-lifecycle"
	cfg.QueuePath = filepath.Join(t.TempDir(), "inbox")
	srv := New(cfg, db, rd, td, zap.NewNop())
	srv.fleetCatalog, _ = fleet.Load("../../../01-data-chain/simulator/fleet.json")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- srv.Run(ctx) }()
	s02wait(t, func() bool { srv.startupMu.Lock(); defer srv.startupMu.Unlock(); return srv.restoreOK })
	ticket := strings.Repeat("b", 48)
	s02must(t, rd.Set(context.Background(), "ws-ticket:"+ticket, "1", time.Minute).Err())
	ws, _, e := websocket.DefaultDialer.Dial("ws://"+cfg.HTTPAddr+"/ws/realtime?ticket="+ticket, nil)
	s02must(t, e)
	defer ws.Close()
	start := time.Now()
	cancel()
	select {
	case e := <-done:
		s02must(t, e)
	case <-time.After(shutdownBudget + time.Second):
		t.Fatal("Run shutdown budget")
	}
	ws.SetReadDeadline(time.Now().Add(time.Second))
	if _, _, e := ws.ReadMessage(); e == nil {
		t.Fatal("hijacked socket survived shutdown")
	}
	select {
	case <-brokerDone:
	case <-time.After(time.Second):
		t.Fatal("MQTT connection survived shutdown")
	}
	if time.Since(start) > shutdownBudget {
		t.Fatal("total lifecycle exit exceeded budget")
	}
	// A late hijack after hub shutdown must close immediately and cannot add a writer.
	late := httptest.NewServer(srv.Handler())
	defer late.Close()
	ticket = strings.Repeat("c", 48)
	s02must(t, rd.Set(context.Background(), "ws-ticket:"+ticket, "1", time.Minute).Err())
	c, _, e := websocket.DefaultDialer.Dial("ws"+strings.TrimPrefix(late.URL, "http")+"/ws/realtime?ticket="+ticket, nil)
	if e == nil {
		defer c.Close()
		c.SetReadDeadline(time.Now().Add(time.Second))
		if _, _, e = c.ReadMessage(); e == nil {
			t.Fatal("late connection stayed open")
		}
	}
	srv.clientsMu.Lock()
	remaining := len(srv.clients)
	srv.clientsMu.Unlock()
	if remaining != 0 {
		t.Fatal("late client leaked")
	}
	t.Logf("actual Run with isolated MQTT protocol peer: HTTP, workers, MQTT and hijacked WebSocket exit %s; late admission rejected", time.Since(start))
}

type s02SlowListener struct {
	net.Listener
	slow *atomic.Bool
}

func (l *s02SlowListener) Accept() (net.Conn, error) {
	c, e := l.Listener.Accept()
	if e != nil {
		return nil, e
	}
	return &s02SlowConn{Conn: c, slow: l.slow.Load()}, nil
}

type s02SlowConn struct {
	net.Conn
	slow     bool
	upgraded bool
	mu       sync.Mutex
	deadline time.Time
}

func (c *s02SlowConn) SetWriteDeadline(at time.Time) error {
	c.mu.Lock()
	c.deadline = at
	c.mu.Unlock()
	return c.Conn.SetWriteDeadline(at)
}
func (c *s02SlowConn) Write(p []byte) (int, error) {
	if c.slow && c.upgraded {
		c.mu.Lock()
		at := c.deadline
		c.mu.Unlock()
		if at.IsZero() {
			return 0, errors.New("fixture missing write deadline")
		}
		timer := time.NewTimer(time.Until(at))
		defer timer.Stop()
		<-timer.C
		return 0, &net.OpError{Op: "write", Net: "tcp", Err: os.ErrDeadlineExceeded}
	}
	n, e := c.Conn.Write(p)
	if strings.Contains(string(p), "101 Switching Protocols") {
		c.upgraded = true
	}
	return n, e
}

// Test the modified CLI startup path, rather than substituting direct SQL for it.
func TestTRS02StartupEntry(t *testing.T) {
	dsn := os.Getenv("TR_S02_TEST_DSN")
	if isolated := os.Getenv("TR_S03_TEST_DSN"); isolated != "" {
		dsn = isolated
	}
	if dsn != "postgres://tr_s02@127.0.0.1:15440/tr_s02_test?sslmode=disable" && dsn != "postgres://tr_s03@127.0.0.1:15441/tr_s03_test?sslmode=disable" {
		t.Fatal("dedicated S02 fixture required")
	}
	bin := os.Getenv("TR_S02_API_BIN")
	if bin == "" {
		t.Fatal("built actual API required")
	}
	db, e := gorm.Open(postgres.Open(dsn), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	s02must(t, e)
	pool, _ := db.DB()
	defer pool.Close()
	s02must(t, db.Exec("DROP DATABASE IF EXISTS tr_s02_startup_test WITH (FORCE)").Error)
	s02must(t, db.Exec("CREATE DATABASE tr_s02_startup_test").Error)
	for i := 0; i < 2; i++ {
		ctx, cancel := context.WithTimeout(context.Background(), 35*time.Second)
		cmd := exec.CommandContext(ctx, bin, "-migrate-only")
		cmd.Dir = "../.."
		cmd.Env = append(os.Environ(), "POSTGRES_HOST=127.0.0.1", "POSTGRES_PORT=15440", "POSTGRES_USER=tr_s02", "POSTGRES_DB=tr_s02_startup_test", "POSTGRES_PASSWORD=fixture-only", "REDIS_PASSWORD=fixture-only", "TDENGINE_ROOT_PASSWORD=fixture-only", "JWT_SECRET="+strings.Repeat("t", 32), "SIMULATION_ALARM_MODE=off", "AGENT_ENABLED=false", "FEISHU_BUSINESS_ENABLED=false")
		out, e := cmd.CombinedOutput()
		cancel()
		if e != nil {
			t.Fatalf("actual CLI startup %d: %v %s", i, e, out)
		}
		if !strings.Contains(string(out), "migration_complete") {
			t.Fatalf("entry did not complete: %s", out)
		}
	}
	t.Log("actual built API -migrate-only fresh and repeat passed under bounded startup context; no Redis/TD/MQTT connection")
}

func TestTRS02GETBoundary(t *testing.T) {
	gin.SetMode(gin.ReleaseMode)
	s := &Server{}
	r := gin.New()
	r.Use(s.alarmMaintenanceGuard)
	r.GET("/api/v1/devices/1/telemetry/export", func(c *gin.Context) {
		if _, ok := c.Get("maintenance_db"); ok {
			t.Fatal("GET acquired a write transaction")
		}
		deadline, ok := c.Request.Context().Deadline()
		if !ok || time.Until(deadline) < 50*time.Second {
			t.Fatal("GET budget was shortened")
		}
		c.Status(204)
	})
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	w := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/api/v1/devices/1/telemetry/export", nil).WithContext(ctx)
	r.ServeHTTP(w, req)
	if w.Code != 204 {
		t.Fatal("GET gate performed a nil-db read")
	}
	// Existing container health uses ping: serving HTTP early must not make
	// that health check pass before recovery/dependencies/freshness are ready.
	srv := New(config.Config{}, nil, nil, nil, zap.NewNop())
	srv.runContext = context.Background()
	srv.setStartup("restoring", false)
	for _, tc := range []struct {
		path   string
		status int
	}{{"/api/v1/ping", 503}, {"/ready", 503}, {"/live", 200}} {
		w := httptest.NewRecorder()
		srv.Handler().ServeHTTP(w, httptest.NewRequest("GET", tc.path, nil))
		if w.Code != tc.status {
			t.Fatalf("startup %s=%d", tc.path, w.Code)
		}
	}
}
