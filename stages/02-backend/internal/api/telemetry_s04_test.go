//go:build tr_s04 && windows

package api

import (
	"context"
	"encoding/json"
	"fmt"
	"github.com/gin-gonic/gin"
	"math"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"powersystem/backend/internal/energy"
	"powersystem/backend/internal/fleet"
	"powersystem/backend/internal/service"
	"runtime"
	"sort"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"
	"unsafe"

	mqtt "github.com/eclipse/paho.mqtt.golang"
	"github.com/redis/go-redis/v9"
	"go.uber.org/zap"
	"gorm.io/gorm"
	"powersystem/backend/internal/config"
	"powersystem/backend/internal/ingest"
	"powersystem/backend/internal/model"
	"powersystem/backend/internal/store"
	"powersystem/backend/internal/tdengine"
	"powersystem/backend/internal/telemetry"
)

const s04DSN = "postgres://tr_s04@127.0.0.1:15442/tr_s04_test?sslmode=disable"

type s04Fixture struct {
	t      *testing.T
	db     *gorm.DB
	rd     *redis.Client
	s      *Server
	cfg    config.Config
	cancel context.CancelFunc
	wg     sync.WaitGroup
}

func s04must(t *testing.T, e error) {
	t.Helper()
	if e != nil {
		t.Fatal(e)
	}
}
func s04new(t *testing.T) *s04Fixture {
	t.Helper()
	if os.Getenv("TR_S04_TEST_DSN") != s04DSN {
		t.Fatal("dedicated real S04 dependencies required; no SKIP")
	}
	f := &s04Fixture{t: t}
	f.cfg = config.Config{PostgresDSN: s04DSN, RedisAddr: "127.0.0.1:16392", TDURL: "http://127.0.0.1:16042", TDUser: "root", TDPassword: "taosdata", TDDatabase: "tr_s04", Broker: "tcp://127.0.0.1:11884", ClientID: "tr-s04-api", JWTSecret: strings.Repeat("s", 32), SimulationAlarmMode: "off"}
	var e error
	f.db, e = store.Postgres(f.cfg)
	s04must(t, e)
	p, _ := f.db.DB()
	t.Cleanup(func() { p.Close() })
	// Keep each scenario before replacing public in the dedicated disposable database.
	var exists bool
	f.db.Raw("SELECT to_regclass('public.telemetry_inbox') IS NOT NULL").Scan(&exists)
	if exists {
		name := fmt.Sprintf("retained_%d", time.Now().UnixNano())
		s04must(t, f.db.Exec("ALTER SCHEMA public RENAME TO "+name+"; CREATE SCHEMA public").Error)
	}
	bin := os.Getenv("TR_S04_API_BIN")
	if bin == "" {
		t.Fatal("actual API migration binary required")
	}
	for i := 0; i < 2; i++ {
		c := exec.Command(bin, "-migrate-only")
		c.Dir = "../.."
		c.Env = append(os.Environ(), "POSTGRES_HOST=127.0.0.1", "POSTGRES_PORT=15442", "POSTGRES_USER=tr_s04", "POSTGRES_DB=tr_s04_test", "POSTGRES_PASSWORD=fixture-only", "REDIS_PASSWORD=fixture-only", "TDENGINE_ROOT_PASSWORD=fixture-only", "JWT_SECRET="+strings.Repeat("s", 32), "FLEET_CONFIG_PATH=fixture-missing", "AGENT_ENABLED=false", "FEISHU_BUSINESS_ENABLED=false", "SIMULATION_ALARM_MODE=off")
		b, e := c.CombinedOutput()
		if e != nil || !strings.Contains(string(b), "migration_complete") {
			t.Fatalf("actual migration: %v %s", e, b)
		}
	}
	f.rd = redis.NewClient(&redis.Options{Addr: f.cfg.RedisAddr, ContextTimeoutEnabled: true, MaxRetries: -1})
	t.Cleanup(func() { f.rd.Close() })
	s04must(t, f.rd.FlushDB(context.Background()).Err())
	td := tdengine.New(f.cfg)
	_, e = td.SQL(context.Background(), "DROP DATABASE IF EXISTS tr_s04")
	s04must(t, e)
	// Init uses the same statements and client as the application.
	old, e := os.Getwd()
	s04must(t, e)
	s04must(t, os.Chdir("../.."))
	e = td.Init(context.Background())
	s04must(t, os.Chdir(old))
	s04must(t, e)
	f.cfg.QueuePath = filepath.Join(os.Getenv("TR_S04_EVIDENCE"), fmt.Sprintf("mqtt-%d", time.Now().UnixNano()), "api.sqlite")
	f.cfg.ClientID = fmt.Sprintf("tr-s04-api-%d", time.Now().UnixNano())
	f.s = New(f.cfg, f.db, f.rd, td, zap.NewNop())
	ctx, cancel := context.WithCancel(context.Background())
	f.cancel = cancel
	f.s.runContext = ctx
	t.Cleanup(func() {
		cancel()
		f.wg.Wait()
		if f.s.ingressDone != nil {
			<-f.s.ingressDone
		}
		if f.s.realtime.cancel != nil {
			f.s.realtime.cancel()
			<-f.s.realtime.done
		}
	})
	return f
}
func (f *s04Fixture) device(n int) model.Device {
	d := model.Device{DeviceCode: fmt.Sprintf("S04-%04d", n), Name: "S04 isolated", DevType: "inverter", StationCode: "S04"}
	s04must(f.t, f.db.Create(&d).Error)
	s04must(f.t, f.s.td.EnsureDevice(context.Background(), d))
	return d
}
func (f *s04Fixture) sample(d model.Device, run string, seq, ts int64) telemetry.Sample {
	return telemetry.Sample{SchemaVersion: 1, DeviceCode: d.DeviceCode, StationCode: d.StationCode, RunID: run, Seq: seq, TS: ts, Voltage: 400, Current: 100, Temperature: 60, Power: 68.589211, Status: 1}
}
func (f *s04Fixture) item(d model.Device, run string, seq, ts int64) model.Inbox {
	s := f.sample(d, run, seq, ts)
	s04must(f.t, f.s.td.Insert(context.Background(), d, []telemetry.Sample{s}))
	b, _ := json.Marshal(s)
	s04must(f.t, f.db.Exec("INSERT INTO telemetry_inbox(run_id,device_id,seq,ts_ms,payload) VALUES(?,?,?,?,?::jsonb)", run, d.ID, seq, ts, string(b)).Error)
	var r model.Inbox
	s04must(f.t, f.db.Where("device_id=? AND run_id=? AND seq=?", d.ID, run, seq).First(&r).Error)
	return r
}
func (f *s04Fixture) launch(fn func(context.Context)) {
	f.wg.Add(1)
	go func() { defer f.wg.Done(); fn(f.s.runContext) }()
}
func s04cpu() time.Duration {
	var created, exit, kernel, user syscall.Filetime
	syscall.GetProcessTimes(syscall.Handle(^uintptr(0)), &created, &exit, &kernel, &user)
	return time.Duration(kernel.Nanoseconds() + user.Nanoseconds())
}
func s04p99(v []float64) float64 {
	if len(v) == 0 {
		return -1
	}
	sort.Float64s(v)
	return v[(len(v)-1)*99/100]
}

// The base profile is the actual 140 business + 3 seeded baseline catalog.
// Extra headroom identities exist only in this isolated in-memory catalog.
func (f *s04Fixture) loadDevices(count int) []model.Device {
	path := os.Getenv("TR_S04_FLEET")
	if path == "" {
		path = "../../../01-data-chain/simulator/fleet.json"
	}
	catalog, e := fleet.Load(path)
	s04must(f.t, e)
	if count > 143 {
		catalog.Stations = append(catalog.Stations, fleet.Station{Code: "S04-HEADROOM", Name: "isolated headroom", Scope: fleet.ScopeBusiness, Kind: "pv", DevicePrefix: "INV", ExpectedDevices: count - 143, RatedPowerKW: 100, Profile: "generic-pv-100kw", GroupSize: 8})
	}
	identities, e := service.SimulationCatalogIdentities(catalog)
	s04must(f.t, e)
	if len(identities) != count {
		f.t.Fatal("profile count", len(identities), count)
	}
	devices := make([]model.Device, 0, count)
	for _, id := range identities {
		d := model.Device{DeviceCode: id.DeviceCode, Name: "S04 isolated " + id.DeviceCode, StationCode: id.StationCode, DevType: "inverter", Vendor: "synthetic", IsSimulated: true, ReferenceVendor: "Huawei", ReferenceModel: "SUN2000-100KTL-M2"}
		s04must(f.t, f.db.Where("device_code=?", id.DeviceCode).FirstOrCreate(&d).Error)
		s04must(f.t, f.s.td.EnsureDevice(context.Background(), d))
		devices = append(devices, d)
	}
	report, e := service.EnsureSimulationPolicyDevices(f.db, catalog)
	s04must(f.t, e)
	if report.Verified != count {
		f.t.Fatal("registry mismatch", report)
	}
	s04must(f.t, store.Migrate(f.db, "../../deploy/postgres/012_natural_alarm_rules.sql"))
	if count > 143 {
		s04must(f.t, f.db.Exec(`INSERT INTO alarm_rules(device_id,metric,operator,threshold,level,enabled,version) SELECT d.id,r.metric,'>',r.threshold,'minor',true,'s04-isolated-headroom' FROM devices d CROSS JOIN(VALUES('temperature',55.0),('voltage',430.0),('current',180.0),('power',110.0))r(metric,threshold) WHERE d.station_code='S04-HEADROOM' ON CONFLICT(device_id,metric) DO NOTHING`).Error)
	}
	f.s.fleetCatalog = catalog
	expected, known, e := f.s.expectedTelemetryDevices(context.Background(), time.Now())
	s04must(f.t, e)
	if len(expected) != count || !known {
		f.t.Fatal("authoritative expected set", len(expected), known)
	}
	var rules int64
	s04must(f.t, f.db.Model(&model.AlarmRule{}).Count(&rules).Error)
	if rules != int64(count*4) {
		f.t.Fatal("representative rules absent", rules)
	}
	return devices
}
func TestTRS04Load(t *testing.T) {
	for _, count := range []int{143, 286} {
		t.Run(fmt.Sprint(count), func(t *testing.T) {
			f := s04new(t)
			ctx := f.s.runContext
			devices := f.loadDevices(count)
			// Segment costs are measured against the actual dependencies, no protocol stand-ins.
			costs := map[string]float64{}
			for _, name := range []string{"pg_roundtrip", "pg_gate_device_lock", "td_exact_check", "redis_roundtrip", "broadcast_enqueue"} {
				start := time.Now()
				for i := 0; i < 30; i++ {
					switch name {
					case "pg_roundtrip":
						s04must(t, f.db.Exec("SELECT 1").Error)
					case "pg_gate_device_lock":
						s04must(t, f.db.Transaction(func(tx *gorm.DB) error {
							if e := service.AlarmWriteGate(tx); e != nil {
								return e
							}
							return service.TelemetryDeviceLock(tx, devices[0].ID)
						}))
					case "td_exact_check":
						_, e := f.s.td.HasSample(ctx, devices[0].ID, time.Now().UnixMilli(), 1)
						s04must(t, e)
					case "redis_roundtrip":
						s04must(t, f.rd.Ping(ctx).Err())
					case "broadcast_enqueue":
						f.s.broadcast(map[string]any{"type": "fixture"})
					}
				}
				costs[name] = float64(time.Since(start).Microseconds()) / 30 / 1000
			}
			s04must(t, f.s.subscribe(ctx))
			gcfg := f.cfg
			gcfg.ClientID = f.cfg.ClientID + "-gateway"
			gcfg.QueuePath = filepath.Join(filepath.Dir(f.cfg.QueuePath), "gateway.sqlite")
			g, e := ingest.NewGateway(gcfg, f.db, f.s.td, zap.NewNop())
			s04must(t, e)
			defer g.Close()
			f.launch(func(c context.Context) {
				if e := g.Run(c); e != nil {
					t.Error(e)
				}
			})
			s04must(t, f.s.restoreLatest(ctx))
			f.s.setStartup("running", true)
			f.launch(f.s.telemetryWatchdog)
			f.launch(f.s.energyWorker)
			f.launch(f.s.worker)
			// S04 realtime worker is registered by the optional fixture hook after implementation.
			if s04RealtimeHook != nil {
				f.launch(func(c context.Context) { s04RealtimeHook(f.s, c) })
			}
			pub := mqtt.NewClient(mqtt.NewClientOptions().AddBroker(f.cfg.Broker).SetClientID(f.cfg.ClientID + "-publisher").SetOrderMatters(true))
			tok := pub.Connect()
			if !tok.WaitTimeout(5*time.Second) || tok.Error() != nil {
				t.Fatal("MQTT publisher", tok.Error())
			}
			defer pub.Disconnect(100)
			time.Sleep(time.Second)
			start := time.Now()
			cpu := s04cpu()
			var latestHeap uint64
			var lag []float64
			var receipt []float64
			var sampleLag []float64
			seen := map[int64]bool{}
			peakPending := int64(0)
			var peakGoroutines int
			stopAt := start.Add(40 * time.Second)
			next := start
			round := int64(0)
			measureStart := start.Add(10 * time.Second)
			for time.Now().Before(stopAt) {
				if !time.Now().Before(next) {
					round++
					for _, i := range randS04Order(count) {
						d := devices[i]
						sample := f.sample(d, "load", round, time.Now().UnixMilli())
						sample.Temperature = 45
						b, _ := json.Marshal(sample)
						token := pub.Publish("device/telemetry", 1, false, b)
						if !token.WaitTimeout(2*time.Second) || token.Error() != nil {
							t.Fatal("publish", token.Error())
						}
					}
					next = next.Add(5 * time.Second)
				}
				var rows []model.Inbox
				s04must(t, f.db.Where("run_id='load'").Find(&rows).Error)
				pending := int64(0)
				for _, r := range rows {
					if r.ProcessedAt == nil {
						pending++
					}
					if r.ProcessedAt != nil && r.ProcessedAt.After(measureStart) && !seen[r.ID] {
						seen[r.ID] = true
						lag = append(lag, float64(r.ProcessedAt.UnixMilli()-r.TS))
						receipt = append(receipt, float64(r.ReceivedAt.UnixMilli()-r.TS))
					}
				}
				if pending > peakPending {
					peakPending = pending
				}
				if time.Now().After(measureStart) {
					for _, d := range devices {
						b, e := f.rd.Get(ctx, fmt.Sprintf("device:%d:latest", d.ID)).Bytes()
						if e == nil {
							var v struct {
								TS int64 `json:"ts_ms"`
							}
							json.Unmarshal(b, &v)
							sampleLag = append(sampleLag, float64(time.Now().UnixMilli()-v.TS))
						}
					}
				}
				var m runtime.MemStats
				runtime.ReadMemStats(&m)
				if m.HeapAlloc > latestHeap {
					latestHeap = m.HeapAlloc
				}
				if n := runtime.NumGoroutine(); n > peakGoroutines {
					peakGoroutines = n
				}
				time.Sleep(50 * time.Millisecond)
			}
			// Close the complete emitted cohort, with the existing 10s E2E budget.
			// This reports every expected sample, including failures, instead of silently
			// dropping the right-censored tail at a measurement-window boundary.
			var measurementPending int64
			s04must(t, f.db.Model(&model.Inbox{}).Where("processed_at IS NULL").Count(&measurementPending).Error)
			cohortDeadline := stopAt.Add(10 * time.Second)
			expectedCohort := count * 6
			var cohort []model.Inbox
			var cohortLag, tdLag, businessLag []float64
			for {
				s04must(t, f.db.Where("run_id='load' AND ts_ms>=?", measureStart.UnixMilli()).Find(&cohort).Error)
				done := 0
				for _, r := range cohort {
					if r.ProcessedAt != nil {
						done++
					}
				}
				if done == expectedCohort || !time.Now().Before(cohortDeadline) {
					break
				}
				time.Sleep(50 * time.Millisecond)
			}
			for _, r := range cohort {
				if r.ProcessedAt != nil {
					cohortLag = append(cohortLag, float64(r.ProcessedAt.UnixMilli()-r.TS))
					if r.TDConfirmedAt != nil {
						tdLag = append(tdLag, float64(r.TDConfirmedAt.Sub(r.ReceivedAt).Microseconds())/1000)
						businessLag = append(businessLag, float64(r.ProcessedAt.Sub(*r.TDConfirmedAt).Microseconds())/1000)
					}
				}
			}
			receipt = nil
			for _, r := range cohort {
				receipt = append(receipt, float64(r.ReceivedAt.UnixMilli()-r.TS))
			}
			f.s.probeTelemetry(ctx)
			f.s.telemetry.mu.Lock()
			expectedCount, classificationKnown, freshRatio := f.s.telemetry.values["expected_devices"], f.s.telemetry.values["classification_known"], f.s.telemetry.values["fresh_ratio"]
			f.s.telemetry.mu.Unlock()
			cpuUsed := s04cpu() - cpu
			var endPending int64
			s04must(t, f.db.Model(&model.Inbox{}).Where("processed_at IS NULL").Count(&endPending).Error)
			pool, _ := f.db.DB()
			poolStats := pool.Stats()
			result := map[string]any{"pg_pool_stats": poolStats, "devices": count, "input_per_second": float64(count) / 5, "expected_devices": expectedCount, "classification_known": classificationKnown, "fresh_ratio": freshRatio, "rules_per_device": 4, "energy_watchdog_enabled": true, "warmup_seconds": 10, "measurement_seconds": 30, "elapsed_seconds": time.Since(start).Seconds(), "business_p99_ms": s04p99(cohortLag), "completion_window_p99_ms": s04p99(lag), "completion_window_throughput": float64(len(lag)) / 30, "expected_cohort": expectedCohort, "received_cohort": len(cohort), "completed_cohort": len(cohortLag), "cohort_td_confirm_p99_ms": s04p99(tdLag), "cohort_business_after_td_p99_ms": s04p99(businessLag), "mqtt_receive_p99_ms": s04p99(receipt), "snapshot_age_p99_ms": s04p99(sampleLag), "observed_business_samples": len(lag), "business_throughput": float64(len(cohortLag)) / 30, "peak_pending": peakPending, "end_pending": endPending, "measurement_end_pending": measurementPending, "cpu_seconds": cpuUsed.Seconds(), "cpu_one_core_percent": cpuUsed.Seconds() / time.Since(start).Seconds() * 100, "go_peak_heap_bytes": latestHeap, "process_peak_rss_bytes": s04rss(), "peak_goroutines": peakGoroutines, "segment_mean_ms": costs, "path_timings": f.s.telemetry.durations()}
			b, _ := json.MarshalIndent(result, "", "  ")
			t.Log(string(b))
			s04must(t, os.WriteFile(filepath.Join(os.Getenv("TR_S04_EVIDENCE"), fmt.Sprintf("%s-load-%d.json", os.Getenv("TR_S04_RUN"), count)), b, 0644))
			f.cancel()
			f.wg.Wait()
			if os.Getenv("TR_S04_RUN") != "baseline" && (expectedCount != float64(count) || classificationKnown != 1 || freshRatio != 1 || s04p99(cohortLag) > 10000 || s04p99(sampleLag) > 15000 || endPending > int64(count) || measurementPending > int64(count) || len(cohortLag) != expectedCohort || latestHeap > 256<<20 || s04rss() > 256<<20 || cpuUsed.Seconds()/time.Since(start).Seconds() > 4 || peakGoroutines > 512) {
				t.Fatal("frozen load budget failed")
			}
		})
	}
}

// Fixed reproducible permutation: coprime stride gives mixed device order.
func randS04Order(n int) []int {
	out := make([]int, n)
	for i := range out {
		out[i] = (i * 37) % n
	}
	return out
}

var s04RealtimeHook func(*Server, context.Context)

func init() { s04RealtimeHook = func(s *Server, c context.Context) { s.snapshotWorker(c) } }

func TestTRS04Single(t *testing.T) {
	f := s04new(t)
	d := f.device(1)
	i := f.item(d, "one", 1, time.Now().UnixMilli())
	p, valid := validSnapshotItem(d, i)
	t.Logf("valid=%v received=%s ts=%d sample=%+v", valid, i.ReceivedAt, i.TS, p)
	s04must(t, f.s.refreshDeviceSnapshot(context.Background(), d))
	var n int64
	f.db.Table("telemetry_snapshots").Count(&n)
	if n != 1 {
		t.Fatal("missing source", n)
	}
	t.Log(f.rd.Get(context.Background(), fmt.Sprintf("device:%d:latest", d.ID)).Result())
}

func TestTRS04T02(t *testing.T) {
	f := s04new(t)
	ctx := context.Background()
	a, b, c := f.device(1), f.device(2), f.device(3)
	ts := time.Now().UnixMilli()
	ai := f.item(a, "batch", 1, ts)
	bi := f.item(b, "batch", 1, ts+1)
	ci := f.item(c, "batch", 1, ts+2)
	an := f.item(a, "next-run", 1, ts+3)
	// Missing exact seq, another device/ts exists; missing must not be confirmed.
	_, e := f.s.td.SQL(ctx, fmt.Sprintf("DELETE FROM %s WHERE ts=%d", f.s.td.Table(b.ID), bi.TS))
	s04must(t, e)
	s04must(t, f.s.td.Insert(ctx, b, []telemetry.Sample{f.sample(b, "batch", 999, bi.TS)}))
	// One device table is absent: partial dependency failure cannot confirm that row.
	_, e = f.s.td.SQL(ctx, "DROP TABLE "+f.s.td.Table(c.ID))
	s04must(t, e)
	f.s.processInboxItem(ctx, an)
	if s04state(f, an.ID) != "pending" {
		t.Fatal("successor overtook durable head")
	}
	f.s.processPending(ctx)
	if s04state(f, ai.ID) != "success" || s04state(f, bi.ID) != "retry" || s04state(f, ci.ID) != "retry" {
		t.Fatal("partial results", s04state(f, ai.ID), s04state(f, bi.ID), s04state(f, ci.ID))
	}
	if s04row(f, bi.ID).TDConfirmedAt != nil || s04row(f, ci.ID).TDConfirmedAt != nil {
		t.Fatal("false TD confirmation")
	}
	s04must(t, f.s.td.EnsureDevice(ctx, c))
	s04must(t, f.s.td.Insert(ctx, c, []telemetry.Sample{f.sample(c, "batch", 1, ci.TS)}))
	s04must(t, f.s.td.Insert(ctx, b, []telemetry.Sample{f.sample(b, "batch", 1, bi.TS)}))
	s04must(t, f.db.Exec("UPDATE telemetry_inbox SET next_attempt_at=now()-interval '1 second' WHERE processed_at IS NULL").Error)
	f.s.processPending(ctx)
	f.s.processPending(ctx)
	for _, i := range []model.Inbox{ai, bi, ci, an} {
		if s04state(f, i.ID) != "success" {
			t.Fatal("not recovered", i.ID)
		}
	}
	t.Log("bounded cross-device exact (device,ts,seq) checks; partial missing/wrong seq/SQL failure isolated per row; cross-run ID order retained")
}
func s04row(f *s04Fixture, id int64) model.Inbox {
	var i model.Inbox
	s04must(f.t, f.db.First(&i, id).Error)
	return i
}
func s04state(f *s04Fixture, id int64) string {
	i := s04row(f, id)
	if i.BusinessState == nil {
		return "legacy"
	}
	return *i.BusinessState
}
func s04wait(t *testing.T, budget time.Duration, fn func() bool) {
	t.Helper()
	end := time.Now().Add(budget)
	for time.Now().Before(end) {
		if fn() {
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatal("bounded condition timeout")
}

func TestTRS04T03(t *testing.T) {
	f := s04new(t)
	ctx := f.s.runContext
	ds := f.loadDevices(143)
	now := time.Now()
	for n := range ds {
		samples := make([]telemetry.Sample, 60)
		for k := range samples {
			samples[k] = f.sample(ds[n], "backlog", int64(k+1), now.Add(-time.Duration(360-k*5)*time.Second).UnixMilli())
		}
		s04must(t, f.s.td.Insert(ctx, ds[n], samples))
		var q strings.Builder
		q.WriteString("INSERT INTO telemetry_inbox(run_id,device_id,seq,ts_ms,payload,received_at) VALUES ")
		args := make([]any, 0, len(samples)*6)
		for k, sample := range samples {
			if k > 0 {
				q.WriteString(",")
			}
			q.WriteString("(?,?,?,?,?::jsonb,?)")
			payload, _ := json.Marshal(sample)
			args = append(args, sample.RunID, ds[n].ID, sample.Seq, sample.TS, string(payload), time.UnixMilli(sample.TS))
		}
		s04must(t, f.db.Exec(q.String(), args...).Error)
	}
	var size int64
	f.db.Table("telemetry_inbox").Count(&size)
	if size != 8580 {
		t.Fatal(size)
	}
	// Preserve order evidence without storing samples twice in process memory.
	s04must(t, f.db.Exec(`CREATE TABLE s04_commits(n bigserial,device_id bigint,inbox_id bigint,recorded_at timestamptz DEFAULT clock_timestamp()); CREATE FUNCTION s04_track() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.business_state='success' AND OLD.processed_at IS NULL THEN INSERT INTO s04_commits(device_id,inbox_id) VALUES(NEW.device_id,NEW.id); END IF; RETURN NEW; END $$; CREATE TRIGGER s04_track AFTER UPDATE ON telemetry_inbox FOR EACH ROW EXECUTE FUNCTION s04_track()`).Error)
	start := time.Now()
	f.launch(f.s.telemetryWatchdog)
	f.launch(f.s.energyWorker)
	f.launch(f.s.worker)
	f.launch(f.s.snapshotWorker)
	// Sustained 143/5s input uses the same real MQTT/SQLite/TD/PG chain as T01.
	s04must(t, f.s.subscribe(ctx))
	gcfg := f.cfg
	gcfg.ClientID = f.cfg.ClientID + "-backlog-gateway"
	gcfg.QueuePath = filepath.Join(filepath.Dir(f.cfg.QueuePath), "gateway.sqlite")
	g, e := ingest.NewGateway(gcfg, f.db, f.s.td, zap.NewNop())
	s04must(t, e)
	defer g.Close()
	f.launch(func(c context.Context) {
		if err := g.Run(c); err != nil {
			t.Error(err)
		}
	})
	pub := mqtt.NewClient(mqtt.NewClientOptions().AddBroker(f.cfg.Broker).SetClientID(f.cfg.ClientID + "-backlog-publisher"))
	tok := pub.Connect()
	if !tok.WaitTimeout(5*time.Second) || tok.Error() != nil {
		t.Fatal("backlog MQTT", tok.Error())
	}
	defer pub.Disconnect(100)
	time.Sleep(time.Second)
	f.launch(func(ctx context.Context) {
		seq := int64(0)
		tick := time.NewTicker(5 * time.Second)
		defer tick.Stop()
		for {
			seq++
			for _, n := range randS04Order(len(ds)) {
				if ctx.Err() != nil {
					return
				}
				d := ds[n]
				sample := f.sample(d, "new-input", seq, time.Now().UnixMilli())
				sample.Temperature = 45
				payload, _ := json.Marshal(sample)
				token := pub.Publish("device/telemetry", 1, false, payload)
				if !token.WaitTimeout(2*time.Second) || token.Error() != nil {
					return
				}
			}
			select {
			case <-ctx.Done():
				return
			case <-tick.C:
			}
		}
	})

	var age []float64
	peak := int64(0)
	var remaining, newCount int64
	for time.Since(start) < 120*time.Second {
		s04must(t, f.db.Model(&model.Inbox{}).Where("run_id='backlog' AND processed_at IS NULL").Count(&remaining).Error)
		var pending int64
		f.db.Model(&model.Inbox{}).Where("processed_at IS NULL").Count(&pending)
		if pending > peak {
			peak = pending
		}
		if time.Since(start) > 10*time.Second {
			for _, d := range ds {
				raw, e := f.rd.Get(ctx, fmt.Sprintf("device:%d:latest", d.ID)).Bytes()
				if e == nil {
					var v struct {
						TS int64 `json:"ts_ms"`
					}
					json.Unmarshal(raw, &v)
					age = append(age, float64(time.Now().UnixMilli()-v.TS))
				}
			}
		}
		if remaining == 0 && pending <= 143 {
			break
		}
		time.Sleep(100 * time.Millisecond)
	}
	elapsed := time.Since(start).Seconds()
	f.cancel()
	f.wg.Wait()
	s04must(t, f.db.Model(&model.Inbox{}).Where("run_id='new-input'").Count(&newCount).Error)
	var processed int64
	f.db.Model(&model.Inbox{}).Where("business_state='success'").Count(&processed)
	gross := float64(processed) / elapsed
	input := float64(newCount) / elapsed
	net := gross - input
	eta := -1.0
	if net > 0 {
		eta = 8580 / net
	}
	var inversions int64
	s04must(t, f.db.Raw(`SELECT count(*) FROM(SELECT inbox_id,lag(inbox_id) OVER(PARTITION BY device_id ORDER BY n) AS prior FROM s04_commits)t WHERE inbox_id<=prior`).Scan(&inversions).Error)
	var worstInterval float64
	s04must(t, f.db.Raw(`SELECT COALESCE(max(extract(epoch from recorded_at-prior)),0) FROM(SELECT recorded_at,lag(recorded_at)OVER(PARTITION BY device_id ORDER BY n) AS prior FROM s04_commits)x`).Scan(&worstInterval).Error)
	result := map[string]any{"initial_backlog": 8580, "remaining_backlog": remaining, "elapsed_seconds": elapsed, "gross_per_second": gross, "actual_input_per_second": input, "planned_input_per_second": 28.6, "net_per_second": net, "estimated_seconds": eta, "snapshot_age_p99_ms": s04p99(age), "peak_pending": peak, "inversions": inversions, "worst_device_service_interval_seconds": worstInterval}
	raw, _ := json.MarshalIndent(result, "", "  ")
	s04must(t, os.WriteFile(filepath.Join(os.Getenv("TR_S04_EVIDENCE"), os.Getenv("TR_S04_RUN")+"-backlog.json"), raw, 0644))
	t.Log(string(raw))
	if remaining != 0 || worstInterval > 20 || net <= 0 || inversions != 0 || s04p99(age) > 15000 || s04p99(age) < 0 || input < 25 || eta > elapsed*1.25 || eta < elapsed*.75 {
		t.Fatal("backlog budget failed")
	}
}

type s04Message struct{ b []byte }

func (s04Message) Duplicate() bool   { return false }
func (s04Message) Qos() byte         { return 1 }
func (s04Message) Retained() bool    { return false }
func (s04Message) Topic() string     { return "device/telemetry" }
func (s04Message) MessageID() uint16 { return 1 }
func (m s04Message) Payload() []byte { return m.b }
func (s04Message) Ack()              {}

func TestTRS04T04(t *testing.T) {
	f := s04new(t)
	d := f.device(1)
	ctx := context.Background()
	now := time.Now()
	old := f.item(d, "old-run", 999, now.Add(-time.Second).UnixMilli())
	s04must(t, f.s.refreshDeviceSnapshot(ctx, d))
	newer := f.item(d, "new-run", 1, now.UnixMilli())
	s04must(t, f.s.refreshDeviceSnapshot(ctx, d))
	backfill := f.item(d, "late", 10000, now.Add(-time.Minute).UnixMilli())
	s04must(t, f.s.refreshDeviceSnapshot(ctx, d))
	var pointer int64
	f.db.Raw("SELECT inbox_id FROM telemetry_snapshots WHERE device_id=?", d.ID).Scan(&pointer)
	if pointer != newer.ID {
		t.Fatal("backfill replaced source", pointer, backfill.ID)
	}
	old = s04row(f, old.ID)
	newer = s04row(f, newer.ID)
	op, _ := telemetry.ParseStored(old.Payload)
	np, _ := telemetry.ParseStored(newer.Payload)
	var wg sync.WaitGroup
	for n := 0; n < 30; n++ {
		wg.Add(1)
		go func(n int) {
			defer wg.Done()
			i, p := old, op
			if n%2 == 0 {
				i, p = newer, np
			}
			if _, e := f.s.writeSnapshot(ctx, d, i, p); e != nil {
				t.Error(e)
			}
		}(n)
	}
	wg.Wait()
	check := func() {
		raw, e := f.rd.Get(ctx, fmt.Sprintf("device:%d:latest", d.ID)).Bytes()
		s04must(t, e)
		var m map[string]any
		json.Unmarshal(raw, &m)
		if m["inbox_id"] != fmt.Sprint(newer.ID) || m["business_commit"] != "not_asserted" || m["power"] != np.Power {
			t.Fatal("CAS source mismatch", m)
		}
	}
	check()
	bad, e := f.rd.Get(ctx, fmt.Sprintf("device:%d:latest", d.ID)).Bytes()
	s04must(t, e)
	var corrupt map[string]any
	json.Unmarshal(bad, &corrupt)
	corrupt["power"] = 0.1
	bad, _ = json.Marshal(corrupt)
	s04must(t, f.rd.Set(ctx, fmt.Sprintf("device:%d:latest", d.ID), bad, 0).Err())
	s04must(t, f.s.restoreLatest(ctx))
	check()
	if ttl, e := f.rd.PTTL(ctx, fmt.Sprintf("device:%d:latest", d.ID)).Result(); e != nil || ttl <= 0 || ttl > snapshotCacheTTL {
		t.Fatal("derived cache lifetime unbounded", ttl, e)
	}
	// An independently restored database is authoritative even if a surviving
	// Redis cache has a higher pre-restore ID; no sequence/future timestamp guess.
	key := fmt.Sprintf("device:%d:latest", d.ID)
	raw, e := f.rd.Get(ctx, key).Bytes()
	s04must(t, e)
	var displaced map[string]any
	json.Unmarshal(raw, &displaced)
	displaced["receipt_order"] = "09999999999999999999"
	displaced["inbox_id"] = "9999999999999999999"
	bytes, _ := json.Marshal(displaced)
	s04must(t, f.rd.Set(ctx, key, bytes, 0).Err())
	s04must(t, f.s.restoreLatest(ctx))
	check()
	// Actual stop/start against only the dedicated Redis container. Restart loses
	// its explicitly nonpersistent data, then recovery uses the PG TD source.
	s04docker(t, "restart", "tr-s04-redis")
	s04wait(t, 5*time.Second, func() bool { return f.rd.Ping(ctx).Err() == nil })
	s04must(t, f.s.restoreLatest(ctx))
	check()
	if s04state(f, newer.ID) != "pending" || s04row(f, newer.ID).TDConfirmedAt == nil {
		t.Fatal("snapshot asserted business success")
	}
	t.Log("cross-run/reset seq, old backfill, concurrent late writes and actual Redis restart; trusted durable TD source matches, business remains pending")
}
func s04docker(t *testing.T, args ...string) {
	t.Helper()
	c := exec.Command("C:/Users/23103/AppData/Local/Programs/DockerDesktop/resources/bin/docker.exe", args...)
	b, e := c.CombinedOutput()
	if e != nil {
		t.Fatalf("dedicated docker %v: %v %s", args, e, b)
	}
}

func TestTRS04T05(t *testing.T) {
	f := s04new(t)
	d := f.device(1)
	ctx := context.Background()
	i := f.item(d, "freeze", 1, time.Now().UnixMilli())
	s04must(t, f.s.refreshDeviceSnapshot(ctx, d))
	newer := f.item(d, "freeze", 2, time.Now().UnixMilli()+1)
	hold := f.db.Begin()
	s04must(t, service.AlarmWriteGate(hold))
	f.s.processInboxItem(ctx, i)
	frozen := make(chan error, 1)
	go func() { frozen <- f.db.Exec("UPDATE alarm_maintenance_state SET enabled=true WHERE id=1").Error }()
	select {
	case <-frozen:
		t.Fatal("freeze bypassed admitted work")
	case <-time.After(50 * time.Millisecond):
	}
	s04must(t, hold.Commit().Error)
	s04must(t, <-frozen)
	if e := f.s.refreshDeviceSnapshot(ctx, d); e == nil {
		t.Fatal("real-time bypassed maintenance")
	}
	f.s.processInboxItem(ctx, newer)
	if s04state(f, newer.ID) != "pending" {
		t.Fatal("history bypassed maintenance")
	}
	if e := f.s.restoreLatest(ctx); e == nil {
		t.Fatal("restore bypassed freeze")
	}
	// Real pg_dump while frozen; retain the file and restore it to a separate DB.
	path := filepath.Join(os.Getenv("TR_S04_EVIDENCE"), os.Getenv("TR_S04_RUN")+"-frozen.dump")
	cmd := exec.Command("C:/Users/23103/AppData/Local/Programs/DockerDesktop/resources/bin/docker.exe", "exec", "tr-s04-pg", "pg_dump", "-U", "tr_s04", "-d", "tr_s04_test", "-n", "public", "-Fc")
	file, e := os.Create(path)
	s04must(t, e)
	cmd.Stdout = file
	cmd.Stderr = os.Stderr
	s04must(t, cmd.Run())
	s04must(t, file.Close())
	restoredName := fmt.Sprintf("tr_s04_restore_%d", time.Now().UnixNano())
	s04docker(t, "exec", "tr-s04-pg", "createdb", "-U", "tr_s04", restoredName)
	restore := exec.Command("C:/Users/23103/AppData/Local/Programs/DockerDesktop/resources/bin/docker.exe", "exec", "-i", "tr-s04-pg", "pg_restore", "-U", "tr_s04", "-d", restoredName, "--no-owner", "--clean", "--if-exists")
	input, e := os.Open(path)
	s04must(t, e)
	restore.Stdin = input
	output, e := restore.CombinedOutput()
	input.Close()
	if e != nil {
		t.Fatalf("frozen restore %v %s", e, output)
	}
	rcfg := f.cfg
	rcfg.PostgresDSN = strings.Replace(s04DSN, "tr_s04_test", restoredName, 1)
	restored, e := store.Postgres(rcfg)
	s04must(t, e)
	rp, _ := restored.DB()
	defer rp.Close()
	var enabled bool
	var source int64
	s04must(t, restored.Raw("SELECT enabled FROM alarm_maintenance_state WHERE id=1").Scan(&enabled).Error)
	s04must(t, restored.Raw("SELECT inbox_id FROM telemetry_snapshots WHERE device_id=?", d.ID).Scan(&source).Error)
	if !enabled || source != i.ID {
		t.Fatal("frozen restored source/fence", enabled, source)
	}

	cancelled, cancel := context.WithCancel(ctx)
	cancel()
	if e := f.s.snapshotSweep(cancelled); e == nil {
		t.Fatal("cancel ignored")
	}
	s04must(t, f.db.Exec("UPDATE alarm_maintenance_state SET enabled=false WHERE id=1").Error)
	s04must(t, f.s.refreshDeviceSnapshot(ctx, d))
	f.s.processInboxItem(ctx, newer)
	if s04state(f, newer.ID) != "success" {
		t.Fatal("resume failed")
	}
	// Four-state maintenance is authoritative for both channels.
	s04must(t, f.db.Exec("DELETE FROM alarm_maintenance_state WHERE id=1").Error)
	if e := f.s.refreshDeviceSnapshot(ctx, d); e == nil {
		t.Fatal("missing singleton admitted")
	}
	t.Log("real PG shared-lock drain, frozen pg_dump retained, both channels/restore reject enabled and missing, cancellation and resume verified; no production backup claim")
}

func TestTRS04T06(t *testing.T) {
	f := s04new(t)
	d := f.device(1)
	now := time.Now()
	status, power := 1, 68.0
	cases := []struct {
		name            string
		sample, receive time.Time
		state           string
		online          bool
	}{{"slow30", now.Add(-30 * time.Second), now, "unknown", false}, {"fast30", now.Add(30 * time.Second), now, "online", true}, {"fast61", now.Add(61 * time.Second), now, "unknown", false}, {"stagnant", now.Add(-time.Minute), now, "unknown", false}, {"future_receive", now, now.Add(time.Second), "unknown", false}, {"old_new_receive", now.Add(-time.Hour), now, "unknown", false}, {"offline", now.Add(-time.Minute), now.Add(-time.Minute), "offline", false}}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			ts := tc.sample.UnixMilli()
			v := overviewSnapshot{ReceivedAt: &tc.receive, TS: &ts, Status: &status, Power: &power}
			raw, _ := json.Marshal(v)
			s04must(t, f.rd.Set(context.Background(), fmt.Sprintf("device:%d:latest", d.ID), raw, 0).Err())
			h := f.s.scoreDevice(context.Background(), d, "")
			if h.State != tc.state || snapshotIsCurrent(v, now) != tc.online {
				t.Fatal("clock disagreement", h, snapshotIsCurrent(v, now))
			}
			f.s.probeTelemetry(context.Background())
			f.s.telemetry.mu.Lock()
			fresh := f.s.telemetry.values["fresh_devices"]
			f.s.telemetry.mu.Unlock()
			wantFresh := float64(0)
			if tc.online {
				wantFresh = 1
			}
			if fresh != wantFresh {
				t.Fatal("watchdog clock disagreement", fresh, wantFresh)
			}
			acc := accumulateOverview([]model.Device{d}, map[int64][]byte{d.ID: raw}, nil, nil, now)
			if (acc.online == 1) != tc.online {
				t.Fatal("overview disagreement")
			}
			// Actual legacy summary handler against the same PG/Redis values.
			r := httptest.NewRecorder()
			c, _ := gin.CreateTestContext(r)
			c.Request = httptest.NewRequest("GET", "/api/v1/dashboard/summary", nil)
			f.s.dashboard(c)
			var out struct {
				Data map[string]any `json:"data"`
			}
			s04must(t, json.Unmarshal(r.Body.Bytes(), &out))
			expected := float64(0)
			if tc.online {
				expected = 1
			}
			if out.Data["online"] != expected || out.Data["fault"] != float64(0) {
				t.Fatal("summary disagreement", r.Code, r.Body.String())
			}
		})
	}
	t.Log("UTC millisecond slow/fast/stall/future receipt/backfill cases across summary, overview and health; unknown never hardware fault")
}

func TestTRS04T07(t *testing.T) {
	f := s04new(t)
	d := f.device(1)
	ctx := context.Background()
	s04must(t, f.db.Create(&model.AlarmRule{DeviceID: d.ID, Metric: "temperature", Operator: ">", Threshold: 50, Level: "major", Enabled: true, Version: "1"}).Error)
	base := time.Now().Add(-5 * time.Minute).UnixMilli()
	a := f.item(d, "energy", 1, base)
	b := f.item(d, "energy", 2, base+5000)
	gap := f.item(d, "energy", 3, base+35000)
	quarantine := f.item(d, "energy", 4, base+40000)
	s04must(t, f.s.refreshDeviceSnapshot(ctx, d))
	for _, i := range []model.Inbox{a, b, gap} {
		f.s.processInboxItem(ctx, i)
		if s04state(f, i.ID) != "success" {
			t.Fatal("history not committed")
		}
		if s04row(f, i.ID).AlarmEvaluation == nil || *s04row(f, i.ID).AlarmEvaluation != "historical_suppressed" {
			t.Fatal("history alarm gap hidden")
		}
	}
	s04must(t, f.db.Exec("UPDATE telemetry_inbox SET business_state='quarantined',processed_at=now(),failure_code='td_missing' WHERE id=?", quarantine.ID).Error)
	s04must(t, energy.MarkDirty(f.db, d.ID, quarantine.TS))
	rt := f.device(2)
	pending := f.item(rt, "realtime-energy", 1, time.Now().UnixMilli())
	s04must(t, f.s.refreshDeviceSnapshot(ctx, rt))
	if s04row(f, pending.ID).TDConfirmedAt == nil || s04state(f, pending.ID) != "pending" {
		t.Fatal("fixture is not realtime-only")
	}
	s04must(t, energy.MarkDirty(f.db, rt.ID, pending.TS))
	_, e := energy.RebuildPending(ctx, f.db, 12)
	s04must(t, e)
	var realtimeEnergy float64
	s04must(t, f.db.Raw("SELECT kwh FROM energy_daily WHERE device_id=?", rt.ID).Scan(&realtimeEnergy).Error)
	if realtimeEnergy != 0 {
		t.Fatal("realtime-only energy fabricated")
	}

	var row struct {
		KWh                           float64 `gorm:"column:kwh"`
		SourceQuality                 string
		CoveredMS, GapCount, MaxGapMS int64
	}
	s04must(t, f.db.Raw("SELECT kwh,source_quality,covered_ms,gap_count,max_gap_ms FROM energy_daily WHERE device_id=?", d.ID).Scan(&row).Error)
	expected := 68.589211 * 5 / 3600
	if math.Abs(row.KWh-expected) > 1e-5 || row.SourceQuality != "provisional" || row.CoveredMS != 5000 || row.GapCount != 1 || row.MaxGapMS != 30000 {
		t.Fatal("energy source/gap", row)
	}
	var alarms, outbox, dirty int64
	f.db.Table("alarm_records").Count(&alarms)
	f.db.Table("business_notification_outbox").Count(&outbox)
	f.db.Table("energy_dirty_days").Count(&dirty)
	if alarms != 0 || outbox != 0 || dirty != 0 {
		t.Fatal("history external effects/dirty", alarms, outbox, dirty)
	}
	rows, e := f.s.energyReportRows([]model.Device{d}, time.UnixMilli(base).In(energyReportZone).Format("2006-01-02"), time.UnixMilli(base).In(energyReportZone).Format("2006-01-02"))
	s04must(t, e)
	if len(rows) != 1 || rows[0].GapCount.Int64 != 1 {
		t.Fatal("report hides gap")
	}
	r := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(r)
	c.Request = httptest.NewRequest("GET", "/api/v1/devices/1/telemetry/progress", nil)
	c.Params = gin.Params{{Key: "id", Value: fmt.Sprint(d.ID)}}
	f.s.telemetryProgress(c)
	if r.Code != 200 || !strings.Contains(r.Body.String(), "historical_suppressed") {
		t.Fatal("alarm history gap API", r.Body.String())
	}
	t.Log("success-only historical integration, pending realtime excluded, quarantine source provisional, 30s gap/covered5s retained; dirty drained, suppressed alarm audit visible, no alarm/outbox generated")
}

func TestTRS04T08(t *testing.T) {
	f := s04new(t)
	d := f.device(1)
	ctx := context.Background()
	i := f.item(d, "outage", 1, time.Now().UnixMilli())
	s04must(t, f.s.refreshDeviceSnapshot(ctx, d))
	s04docker(t, "stop", "tr-s04-redis")
	f.s.processInboxItem(ctx, i)
	if s04state(f, i.ID) != "success" {
		t.Fatal("Redis blocked durable business")
	}
	if e := f.s.refreshDeviceSnapshot(ctx, d); e == nil {
		t.Fatal("Redis outage hidden")
	}
	s04docker(t, "start", "tr-s04-redis")
	s04wait(t, 5*time.Second, func() bool { return f.rd.Ping(ctx).Err() == nil })
	time.Sleep(time.Second)
	s04must(t, f.s.restoreLatest(ctx))
	j := f.item(d, "outage", 2, time.Now().UnixMilli())
	s04docker(t, "stop", "tr-s04-td")
	f.s.processInboxItem(ctx, j)
	if s04state(f, j.ID) != "retry" || s04row(f, j.ID).ProcessedAt != nil {
		t.Fatal("TD outage terminalized")
	}
	s04docker(t, "start", "tr-s04-td")
	s04wait(t, 15*time.Second, func() bool { _, e := f.s.td.SQL(ctx, "SELECT 1"); return e == nil })
	s04must(t, f.db.Exec("UPDATE telemetry_inbox SET next_attempt_at=now()-interval '1 second' WHERE id=?", j.ID).Error)
	time.Sleep(time.Second)
	f.s.processInboxItem(ctx, j)
	if s04state(f, j.ID) != "success" {
		t.Fatal("TD recovery")
	}
	// Actual PostgreSQL interruption also leaves original rows/timestamps intact.
	before := s04row(f, j.ID)
	s04docker(t, "stop", "tr-s04-pg")
	if e := f.s.snapshotSweep(ctx); e == nil {
		t.Fatal("PG outage hidden")
	}
	s04docker(t, "start", "tr-s04-pg")
	pool, _ := f.db.DB()
	s04waitDependency(t, 120*time.Second, func() bool {
		ping, end := context.WithTimeout(ctx, time.Second)
		defer end()
		return pool.PingContext(ping) == nil
	})
	after := s04row(f, j.ID)
	if !before.ReceivedAt.Equal(after.ReceivedAt) || string(before.Payload) != string(after.Payload) {
		t.Fatal("recovery rewrote raw input")
	}
	// Existing-data migration and repeat entry keep both source pointer and audit.
	s04migrate(t, os.Getenv("TR_S04_API_BIN"))
	s04migrate(t, os.Getenv("TR_S04_API_BIN"))
	if s04row(f, j.ID).TDConfirmedAt == nil {
		t.Fatal("migration lost confirmation")
	}
	s04must(t, f.db.Exec("UPDATE telemetry_scheduler SET history_enabled=false,history_parallelism=1 WHERE id=1").Error)
	k := f.item(d, "valve", 1, time.Now().UnixMilli())
	f.s.processPending(ctx)
	if s04state(f, k.ID) != "pending" {
		t.Fatal("valve ignored")
	}
	s04must(t, f.s.refreshDeviceSnapshot(ctx, d))
	s04must(t, f.db.Exec("UPDATE telemetry_scheduler SET history_enabled=true WHERE id=1").Error)
	f.s.processPending(ctx)
	if s04state(f, k.ID) != "success" {
		t.Fatal("valve resume")
	}
	// Demonstrate why the actual pre-S04 API is not a valid rollback writer.
	legacy := f.item(d, "compat-old-worker", 1, time.Now().UnixMilli())
	oldbin := os.Getenv("TR_S04_OLD_API_BIN")
	if oldbin == "" {
		oldbin = filepath.Join(os.Getenv("TR_S04_EVIDENCE"), "api-baseline.exe")
	}
	old := exec.Command(oldbin)
	old.Dir = "../.."
	old.Env = append(os.Environ(), "POSTGRES_HOST=127.0.0.1", "POSTGRES_PORT=15442", "POSTGRES_USER=tr_s04", "POSTGRES_DB=tr_s04_test", "POSTGRES_PASSWORD=fixture-only", "REDIS_ADDR=127.0.0.1:16392", "REDIS_PASSWORD=", "TDENGINE_URL=http://127.0.0.1:16042", "TDENGINE_ROOT_PASSWORD=taosdata", "JWT_SECRET="+strings.Repeat("s", 32), "FLEET_CONFIG_PATH=fixture-missing", "AGENT_ENABLED=false", "FEISHU_BUSINESS_ENABLED=false", "SIMULATION_ALARM_MODE=off", "HTTP_ADDR=127.0.0.1:18887", "MQTT_BROKER=tcp://127.0.0.1:11884", "MQTT_CLIENT_PREFIX=tr-s04-compat", "TDENGINE_DATABASE=tr_s04")
	// Config requires a nonempty password, Redis AUTH would fail; start this old
	// worker with dedicated Redis briefly configured for that fixture-only password.
	s04must(t, f.rd.ConfigSet(ctx, "requirepass", "fixture-only").Err())
	t.Cleanup(func() {
		auth := redis.NewClient(&redis.Options{Addr: f.cfg.RedisAddr, Password: "fixture-only"})
		defer auth.Close()
		auth.ConfigSet(context.Background(), "requirepass", "")
	})
	old.Env = append(old.Env, "REDIS_PASSWORD=fixture-only")
	logFile, e := os.Create(filepath.Join(os.Getenv("TR_S04_EVIDENCE"), os.Getenv("TR_S04_RUN")+"-old-worker.txt"))
	s04must(t, e)
	old.Stdout = logFile
	old.Stderr = logFile
	s04must(t, old.Start())
	defer func() {
		if old.Process != nil {
			old.Process.Kill()
			old.Wait()
		}
		logFile.Close()
	}()
	s04wait(t, 15*time.Second, func() bool { return s04state(f, legacy.ID) == "success" })
	s04must(t, old.Process.Kill())
	old.Wait()
	old.Process = nil
	logFile.Close()
	authRedis := redis.NewClient(&redis.Options{Addr: f.cfg.RedisAddr, Password: "fixture-only"})
	s04must(t, authRedis.ConfigSet(ctx, "requirepass", "").Err())
	authRedis.Close()
	if s04row(f, legacy.ID).AlarmEvaluation != nil {
		t.Fatal("old binary unexpectedly satisfies new audit")
	}
	s04must(t, f.s.restoreLatest(ctx))
	if _, e := f.rd.Get(ctx, fmt.Sprintf("device:%d:latest", d.ID)).Result(); e != nil {
		t.Fatal("same-schema forward recovery", e)
	}
	t.Log("actual dedicated PG/Redis/TD interruption and recovery; empty/existing/repeat application migration; valve pause/resume; actual pre-S04 writer omits audit, rejected as rollback, same-schema candidate recovery succeeds")
}
func s04migrate(t *testing.T, bin string) {
	c := exec.Command(bin, "-migrate-only")
	c.Dir = "../.."
	c.Env = append(os.Environ(), "POSTGRES_HOST=127.0.0.1", "POSTGRES_PORT=15442", "POSTGRES_USER=tr_s04", "POSTGRES_DB=tr_s04_test", "POSTGRES_PASSWORD=fixture-only", "REDIS_PASSWORD=fixture-only", "TDENGINE_ROOT_PASSWORD=fixture-only", "JWT_SECRET="+strings.Repeat("s", 32), "FLEET_CONFIG_PATH=fixture-missing", "AGENT_ENABLED=false", "FEISHU_BUSINESS_ENABLED=false", "SIMULATION_ALARM_MODE=off")
	b, e := c.CombinedOutput()
	if e != nil || !strings.Contains(string(b), "migration_complete") {
		t.Fatalf("actual migration %v %s", e, b)
	}
}

func s04rss() uintptr {
	var counters struct {
		Size                                                                                         uint32
		Faults                                                                                       uint32
		PeakWorkingSet, WorkingSet, PeakPaged, Paged, PeakNonPaged, NonPaged, Pagefile, PeakPagefile uintptr
	}
	counters.Size = uint32(unsafe.Sizeof(counters))
	proc := syscall.NewLazyDLL("psapi.dll").NewProc("GetProcessMemoryInfo")
	ok, _, _ := proc.Call(^uintptr(0), uintptr(unsafe.Pointer(&counters)), uintptr(counters.Size))
	if ok == 0 {
		return ^uintptr(0)
	}
	return counters.PeakWorkingSet
}

func s04waitDependency(t *testing.T, budget time.Duration, fn func() bool) {
	t.Helper()
	end := time.Now().Add(budget)
	for time.Now().Before(end) {
		if fn() {
			return
		}
		time.Sleep(time.Second)
	}
	t.Fatal("dependency did not actually become healthy within declared boot budget")
}
