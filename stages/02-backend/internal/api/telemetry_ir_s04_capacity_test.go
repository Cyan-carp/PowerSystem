//go:build ir_s04

// IR-S04 容量余量的隔离基线夹具（仅本机/一次性 home-dev 依赖）。
//
// 目的：在固定输入与固定环境下量测**当前候选**的真实历史处理路径：
//
//	processPendingBudget -> processInboxItem -> TelemetryTransaction -> ProcessLockedSample
//
// 以及 TD 确认（真实 TDengine REST）、Redis 结果写入（refreshDeviceSnapshot/snapshotSweep）。
//
// 它不修改任何业务源码，只在测试进程内做夹具、计时和统计。运行前必须存在：
//   - 一次性 PG 库 ir_s04_bench（角色 ir_s04）经 127.0.0.1:15443 隧道；
//   - 一次性 Redis 127.0.0.1:16391（无认证）；
//   - 一次性 TDengine 127.0.0.1:16041（root/taosdata，官方镜像默认口令）。
//
// 三个地址都写死并在运行时校验；任何偏离直接 Fatal，不会把 DROP SCHEMA 打到别处。
//
// 场景（IR_S04_SCENARIO）：
//
//	loop-backlog-p8          真实 worker 同款 500ms ticker + 30s tick 预算，8 路历史积压排空
//	loop-backlog-p8-s02on    同上，另起 S02 过期 worker（EXPLICIT on）
//	item-backlog-p8          真实 processInboxItem 8 路并发，逐条记录（带 SQL 计数）
//	item-backlog-p8-silent   同上但关闭 SQL 计数，用于量化计数开销
//	item-backlog-p4 / -p1    并发扩展曲线
//	item-backlog-p8-s03on    S03 熔断 EXPLICIT on（全部命中）
//	item-fresh-p8            新鲜输入（会走告警评估），随后测 Redis 冷/暖写入
//	item-tdmiss-p8-s03off    TD 成功查无（假缺失路径），S03 off
//	item-tdmiss-p8-s03on     TD 成功查无，S03 on
package api

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"runtime/metrics"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	mqtt "github.com/eclipse/paho.mqtt.golang"
	"github.com/redis/go-redis/v9"
	"go.uber.org/zap"
	"go.uber.org/zap/zapcore"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"

	"powersystem/backend/internal/config"
	"powersystem/backend/internal/fleet"
	"powersystem/backend/internal/model"
	"powersystem/backend/internal/store"
	"powersystem/backend/internal/tdengine"
	"powersystem/backend/internal/telemetry"
)

const (
	s04FixtureDSN      = "postgres://ir_s04:ir_s04_fixture_pw@127.0.0.1:15443/ir_s04_bench?sslmode=disable"
	s04FixtureDatabase = "ir_s04_bench"
	s04FixtureRedis    = "127.0.0.1:16391"
	s04FixtureTD       = "http://127.0.0.1:16041"
	s04FixtureTDDb     = "ir_s04"
	s04PoolMax         = 28 // 与 store.Open 的生产连接池一致
)

// ---------- 环境与守卫 ----------

func s04EnvOf(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

type s04Env struct{ dsn, redis, td string }

// s04RequireFixture 只接受本机回环上的一次性夹具：
//   - PG 必须 user=ir_s04、db=ir_s04_bench、host=127.0.0.1；端口允许 15443（本机隧道）
//     或 5432（在 home-dev 上直接运行，应用与依赖同机回环）。
//   - Redis/TD 端口允许一次性容器端口（16391/16041）或 home-dev 原生端口（6379/6041）。
//
// 业务库是 powersystem/powersystem，生产无 ir_s04_bench，因此这组约束不会落到生产。
func s04RequireFixture(t *testing.T) s04Env {
	t.Helper()
	dsn := s04EnvOf("IR_S04_TEST_DSN", s04FixtureDSN)
	u, err := url.Parse(dsn)
	if err != nil || u.Scheme != "postgres" || u.User == nil || u.User.Username() != "ir_s04" ||
		u.Hostname() != "127.0.0.1" || strings.TrimPrefix(u.Path, "/") != s04FixtureDatabase {
		t.Fatalf("IR-S04 refuses DSN %q: need user=ir_s04 db=%s host=127.0.0.1", s04MaskDSN(dsn), s04FixtureDatabase)
	}
	switch u.Port() {
	case "15443", "5432":
	default:
		t.Fatalf("IR-S04 refuses PG port %q (allowed 15443 local tunnel / 5432 on home-dev)", u.Port())
	}
	redisAddr := s04EnvOf("IR_S04_REDIS_ADDR", s04FixtureRedis)
	if redisAddr != "127.0.0.1:16391" && redisAddr != "127.0.0.1:6379" {
		t.Fatalf("IR-S04 refuses Redis %q (allowed 127.0.0.1:16391 fixture / 127.0.0.1:6379 on home-dev)", redisAddr)
	}
	// 16041 是首轮夹具端口；16042 是本轮混合负载专用容器端口（16041 被宿主上
	// 一个无关的 /metrics 抓取器每 0.5 秒探测，换端口是为了不让 404 探测污染 TD
	// 容器 CPU）。6041 是 home-dev 原生 TD。
	td := strings.TrimRight(s04EnvOf("IR_S04_TD_URL", s04FixtureTD), "/")
	if td != "http://127.0.0.1:16041" && td != "http://127.0.0.1:16042" && td != "http://127.0.0.1:6041" {
		t.Fatalf("IR-S04 refuses TD %q (allowed 127.0.0.1:16041/16042 fixture / 127.0.0.1:6041 on home-dev)", td)
	}
	return s04Env{dsn: dsn, redis: redisAddr, td: td}
}

func s04MaskDSN(dsn string) string {
	if i := strings.Index(dsn, "@"); i > 0 {
		if j := strings.Index(dsn, "://"); j >= 0 && j < i {
			return dsn[:j+3] + "***" + dsn[i:]
		}
	}
	return "***"
}

// ---------- 统计 ----------

type s04Latency struct {
	N      int     `json:"n"`
	MinMS  float64 `json:"min_ms"`
	P50MS  float64 `json:"p50_ms"`
	P95MS  float64 `json:"p95_ms"`
	P99MS  float64 `json:"p99_ms"`
	MeanMS float64 `json:"mean_ms"`
	MaxMS  float64 `json:"max_ms"`
}

// s04LatencyOf 用 nearest-rank 口径：p99 = 第 ceil(0.99*N) 个样本（1-based）。
// N<100 时 p99 等于当前最大值，输出中同时给出 n，避免把小样本分位数当成稳定尾部。
func s04LatencyOf(ds []time.Duration) s04Latency {
	out := s04Latency{N: len(ds)}
	if len(ds) == 0 {
		return out
	}
	ms := make([]float64, len(ds))
	var sum float64
	for i, d := range ds {
		ms[i] = float64(d.Microseconds()) / 1000
		sum += ms[i]
	}
	sort.Float64s(ms)
	at := func(p float64) float64 {
		idx := int(math.Ceil(p*float64(len(ms)))) - 1
		if idx < 0 {
			idx = 0
		}
		if idx >= len(ms) {
			idx = len(ms) - 1
		}
		return ms[idx]
	}
	out.MinMS, out.P50MS, out.P95MS, out.P99MS, out.MaxMS = ms[0], at(0.50), at(0.95), at(0.99), ms[len(ms)-1]
	out.MeanMS = sum / float64(len(ms))
	return out
}

func s04CPU() float64 {
	s := []metrics.Sample{
		{Name: "/cpu/classes/user:cpu-seconds"},
		{Name: "/cpu/classes/system:cpu-seconds"},
	}
	metrics.Read(s)
	var total float64
	for _, v := range s {
		if v.Value.Kind() == metrics.KindFloat64 {
			total += v.Value.Float64()
		}
	}
	return total
}

// ---------- PG 语句计数（仅测试内 GORM logger） ----------

type s04QueryCounter struct {
	mu       sync.Mutex
	total    int64
	totalDur time.Duration
	kinds    map[string]*s04KindStat
}

type s04KindStat struct {
	Count int64
	Dur   time.Duration
}

func newS04QueryCounter() *s04QueryCounter {
	return &s04QueryCounter{kinds: map[string]*s04KindStat{}}
}

func (c *s04QueryCounter) LogMode(logger.LogLevel) logger.Interface { return c }
func (c *s04QueryCounter) Info(context.Context, string, ...any)     {}
func (c *s04QueryCounter) Warn(context.Context, string, ...any)     {}
func (c *s04QueryCounter) Error(context.Context, string, ...any)    {}

func (c *s04QueryCounter) Trace(_ context.Context, begin time.Time, fc func() (string, int64), _ error) {
	sqlText, _ := fc()
	kind := s04ClassifySQL(sqlText)
	d := time.Since(begin)
	c.mu.Lock()
	defer c.mu.Unlock()
	c.total++
	c.totalDur += d
	st := c.kinds[kind]
	if st == nil {
		st = &s04KindStat{}
		c.kinds[kind] = st
	}
	st.Count++
	st.Dur += d
}

func (c *s04QueryCounter) Reset() {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.total = 0
	c.totalDur = 0
	c.kinds = map[string]*s04KindStat{}
}

func (c *s04QueryCounter) Snapshot(items int) map[string]any {
	c.mu.Lock()
	defer c.mu.Unlock()
	kinds := map[string]any{}
	for k, v := range c.kinds {
		mean := 0.0
		if v.Count > 0 {
			mean = float64(v.Dur.Microseconds()) / float64(v.Count) / 1000
		}
		kinds[k] = map[string]any{"count": v.Count, "mean_ms": mean, "per_item": s04Ratio(v.Count, int64(items))}
	}
	return map[string]any{
		"statements_total":    c.total,
		"statements_per_item": s04Ratio(c.total, int64(items)),
		"mean_ms":             float64(c.totalDur.Microseconds()) / math.Max(1, float64(c.total)) / 1000,
		"by_kind":             kinds,
	}
}

func s04Ratio(n, d int64) float64 {
	if d <= 0 {
		return 0
	}
	return float64(n) / float64(d)
}

func s04ClassifySQL(q string) string {
	s := strings.ToUpper(strings.ReplaceAll(strings.TrimSpace(q), "\"", ""))
	switch {
	case strings.HasPrefix(s, "BEGIN"), strings.HasPrefix(s, "COMMIT"), strings.HasPrefix(s, "ROLLBACK"):
		return "txn_control"
	case strings.Contains(s, "PG_ADVISORY_XACT_LOCK"):
		return "device_advisory_lock"
	case strings.Contains(s, "FROM TELEMETRY_INBOX WHERE ID=") && strings.Contains(s, "FOR UPDATE"):
		return "inbox_row_for_update"
	case strings.Contains(s, "SELECT ID FROM TELEMETRY_INBOX WHERE DEVICE_ID=") && strings.Contains(s, "ORDER BY ID"):
		return "inbox_head_probe"
	case strings.Contains(s, "FROM DEVICES"):
		return "device_lookup"
	case strings.HasPrefix(s, "UPDATE TELEMETRY_INBOX"):
		return "inbox_state_update"
	case strings.Contains(s, "TELEMETRY_ALARM_PROGRESS"):
		return "alarm_progress_upsert"
	case strings.Contains(s, "FROM ALARM_RULES"):
		return "alarm_rules_select"
	case strings.Contains(s, "FROM ALARM_RECORDS"):
		return "alarm_records_select"
	case strings.Contains(s, "ENERGY_DIRTY_DAYS"):
		return "energy_dirty_upsert"
	case strings.Contains(s, "AVAILABILITY_DIRTY"):
		return "availability_dirty_upsert"
	case strings.Contains(s, "FROM TELEMETRY_SCHEDULER"):
		return "scheduler_read"
	case strings.Contains(s, "UPDATE TELEMETRY_SCHEDULER"):
		return "scheduler_cursor_update"
	case strings.Contains(s, "FROM ALARM_MAINTENANCE_STATE"):
		return "maintenance_gate"
	default:
		fields := strings.Fields(s)
		if len(fields) >= 2 {
			return "other_" + strings.ToLower(fields[0]+"_"+fields[1])
		}
		return "other"
	}
}

// ---------- TD 测速代理 ----------

type s04TDStats struct {
	mu     sync.Mutex
	lat    []time.Duration
	failed int64
}

func (s *s04TDStats) add(d time.Duration, failed bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.lat = append(s.lat, d)
	if failed {
		s.failed++
	}
}

func (s *s04TDStats) reset() ([]time.Duration, int64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	old, failed := s.lat, s.failed
	s.lat, s.failed = nil, 0
	return old, failed
}

// s04StartTDProxy 把 app 的 TD REST 调用原样转发到真实 TDengine，并记录逐请求延迟。
// 代理本身只加一次 loopback HTTP 转发（<1ms 量级），在结果里单独说明。
func s04StartTDProxy(t *testing.T, target string) (*httptest.Server, *s04TDStats) {
	t.Helper()
	stats := &s04TDStats{}
	client := &http.Client{Timeout: 15 * time.Second}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		began := time.Now()
		body, err := io.ReadAll(r.Body)
		failed := err != nil
		status := http.StatusOK
		var payload []byte
		if err == nil {
			req, e := http.NewRequestWithContext(r.Context(), r.Method, strings.TrimRight(target, "/")+r.URL.RequestURI(), bytes.NewReader(body))
			if e != nil {
				failed = true
			} else {
				req.Header = r.Header.Clone()
				resp, e := client.Do(req)
				if e != nil {
					failed = true
				} else {
					defer resp.Body.Close()
					payload, err = io.ReadAll(io.LimitReader(resp.Body, 8<<20))
					if err != nil {
						failed = true
					}
					for k, v := range resp.Header {
						w.Header()[k] = v
					}
					status = resp.StatusCode
				}
			}
		}
		stats.add(time.Since(began), failed)
		if failed {
			status = http.StatusBadGateway
		}
		w.WriteHeader(status)
		_, _ = w.Write(payload)
	}))
	t.Cleanup(srv.Close)
	return srv, stats
}

// ---------- 数据库与夹具 ----------

func s04Open(t *testing.T, dsn string, l logger.Interface) (*gorm.DB, *sql.DB) {
	t.Helper()
	db, err := gorm.Open(postgres.Open(dsn), &gorm.Config{Logger: l, DisableAutomaticPing: true})
	if err != nil {
		t.Fatalf("open fixture PG: %v", err)
	}
	pool, err := db.DB()
	if err != nil {
		t.Fatalf("fixture pool: %v", err)
	}
	pool.SetMaxOpenConns(s04PoolMax)
	pool.SetMaxIdleConns(s04PoolMax)
	var dbName, userName string
	if err := db.Raw("SELECT current_database(), current_user").Row().Scan(&dbName, &userName); err != nil {
		t.Fatalf("fixture identity: %v", err)
	}
	if dbName != s04FixtureDatabase || userName != "ir_s04" {
		t.Fatalf("refuse: connected to %s as %s, expected %s as ir_s04", dbName, userName, s04FixtureDatabase)
	}
	return db, pool
}

func s04ResetSchema(t *testing.T, db *gorm.DB) {
	t.Helper()
	if err := db.Exec("DROP SCHEMA public CASCADE; CREATE SCHEMA public").Error; err != nil {
		t.Fatalf("reset fixture schema: %v", err)
	}
	for _, path := range store.MigrationPaths {
		if err := store.Migrate(db, filepath.Join("../..", path)); err != nil {
			t.Fatalf("migrate %s: %v", path, err)
		}
	}
}

type s04DeviceSpec struct{ Code, Station string }

func s04FleetSpecs(t *testing.T) []s04DeviceSpec {
	t.Helper()
	catalog, err := fleet.Load("../../../01-data-chain/simulator/fleet.json")
	if err != nil {
		t.Fatalf("load fleet.json: %v", err)
	}
	var out []s04DeviceSpec
	for _, st := range catalog.Stations {
		if len(st.Explicit) > 0 {
			for _, code := range st.Explicit {
				out = append(out, s04DeviceSpec{Code: code, Station: st.Code})
			}
			continue
		}
		for i := 1; i <= st.ExpectedDevices; i++ {
			out = append(out, s04DeviceSpec{Code: fmt.Sprintf("%s-%s-%02d", st.Code, st.DevicePrefix, i), Station: st.Code})
		}
	}
	if len(out) != 143 {
		t.Fatalf("fixed input drift: fleet yields %d devices, expected 143 (business 140 + baseline 3)", len(out))
	}
	return out
}

func s04SeedDevices(t *testing.T, db *gorm.DB, specs []s04DeviceSpec) []model.Device {
	t.Helper()
	devices := make([]model.Device, 0, len(specs))
	for _, spec := range specs {
		devices = append(devices, model.Device{
			DeviceCode: spec.Code, Name: spec.Code, DevType: "inverter", StationCode: spec.Station,
			IsSimulated: true, Vendor: "fixture", ReferenceVendor: "fixture", ReferenceModel: "fixture",
		})
	}
	if err := db.CreateInBatches(&devices, 100).Error; err != nil {
		t.Fatalf("seed devices: %v", err)
	}
	rules := make([]model.AlarmRule, 0, len(devices)*4)
	for _, d := range devices {
		rules = append(rules,
			model.AlarmRule{DeviceID: d.ID, Metric: "temperature", Operator: ">", Threshold: 60, Level: "major", Enabled: true, Version: "v1"},
			model.AlarmRule{DeviceID: d.ID, Metric: "voltage", Operator: "<", Threshold: 320, Level: "minor", Enabled: true, Version: "v1"},
			model.AlarmRule{DeviceID: d.ID, Metric: "current", Operator: ">", Threshold: 140, Level: "major", Enabled: true, Version: "v1"},
			model.AlarmRule{DeviceID: d.ID, Metric: "power", Operator: ">", Threshold: 110, Level: "minor", Enabled: true, Version: "v1"},
		)
	}
	if err := db.CreateInBatches(&rules, 300).Error; err != nil {
		t.Fatalf("seed rules: %v", err)
	}
	return devices
}

func s04Sample(runID string, d model.Device, seq, ts int64) telemetry.Sample {
	return telemetry.Sample{
		SchemaVersion: 1, RunID: runID, DeviceCode: d.DeviceCode, StationCode: d.StationCode,
		Seq: seq, TS: ts,
		Voltage: 380 + float64(seq%5), Current: 90, Temperature: 35 + float64(seq%3), Power: 60,
		Status: 1, FaultCode: 0,
	}
}

func s04SeedTD(t *testing.T, td *tdengine.Client, devices []model.Device, runID string, rows int, tdSeqShift int64, tsBase time.Time) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 4*time.Minute)
	defer cancel()
	for _, stmt := range []string{
		"CREATE DATABASE IF NOT EXISTS " + s04FixtureTDDb + " PRECISION 'ms' KEEP 365 DURATION 10",
		"CREATE STABLE IF NOT EXISTS " + s04FixtureTDDb + ".telemetry (ts TIMESTAMP, seq BIGINT, voltage FLOAT, current FLOAT, temperature FLOAT, power FLOAT, status INT, fault_code INT) TAGS (device_id BINARY(64), station_id BINARY(64))",
	} {
		if _, err := td.SQL(ctx, stmt); err != nil {
			t.Fatalf("TD init: %v", err)
		}
	}
	for _, d := range devices {
		samples := make([]telemetry.Sample, 0, rows)
		for i := 0; i < rows; i++ {
			s := s04Sample(runID, d, int64(i+1), tsBase.Add(time.Duration(i)*5*time.Second).UnixMilli())
			s.Seq += tdSeqShift // 假缺失场景：TD 里同 ts 的 seq 不同
			samples = append(samples, s)
		}
		if err := td.Insert(ctx, d, samples); err != nil {
			t.Fatalf("TD seed device %d: %v", d.ID, err)
		}
	}
}

func s04SeedInbox(t *testing.T, db *gorm.DB, devices []model.Device, runID string, rows int, tsBase, receivedAt time.Time) [][]model.Inbox {
	t.Helper()
	type row struct {
		deviceID int64
		seq, ts  int64
		payload  string
	}
	var all []row
	for _, d := range devices {
		for i := 0; i < rows; i++ {
			s := s04Sample(runID, d, int64(i+1), tsBase.Add(time.Duration(i)*5*time.Second).UnixMilli())
			b, err := json.Marshal(s)
			if err != nil {
				t.Fatalf("payload: %v", err)
			}
			all = append(all, row{deviceID: d.ID, seq: s.Seq, ts: s.TS, payload: string(b)})
		}
	}
	const chunk = 200
	for start := 0; start < len(all); start += chunk {
		end := min(start+chunk, len(all))
		var sb strings.Builder
		args := make([]any, 0, (end-start)*6)
		sb.WriteString("INSERT INTO telemetry_inbox(run_id,device_id,seq,ts_ms,payload,received_at) VALUES ")
		for i := start; i < end; i++ {
			if i > start {
				sb.WriteString(",")
			}
			sb.WriteString("(?,?,?,?,?::jsonb,?)")
			args = append(args, runID, all[i].deviceID, all[i].seq, all[i].ts, all[i].payload, receivedAt)
		}
		if err := db.Exec(sb.String(), args...).Error; err != nil {
			t.Fatalf("seed inbox: %v", err)
		}
	}
	var items []model.Inbox
	if err := db.Where("run_id=?", runID).Order("device_id,id").Find(&items).Error; err != nil {
		t.Fatalf("read inbox fixture: %v", err)
	}
	if len(items) != len(devices)*rows {
		t.Fatalf("fixture drift: seeded %d items, expected %d", len(items), len(devices)*rows)
	}
	byDevice := map[int64][]model.Inbox{}
	for _, it := range items {
		byDevice[it.DeviceID] = append(byDevice[it.DeviceID], it)
	}
	out := make([][]model.Inbox, 0, len(devices))
	for _, d := range devices {
		out = append(out, byDevice[d.ID])
	}
	return out
}

// ---------- 结果结构 ----------

type s04PGDelta struct {
	XactCommit   int64 `json:"xact_commit"`
	XactRollback int64 `json:"xact_rollback"`
	BlksRead     int64 `json:"blks_read"`
	BlksHit      int64 `json:"blks_hit"`
	TempBytes    int64 `json:"temp_bytes"`
	Deadlocks    int64 `json:"deadlocks"`
	TupInserted  int64 `json:"tup_inserted"`
	TupUpdated   int64 `json:"tup_updated"`
	TupDeleted   int64 `json:"tup_deleted"`
}

func s04ReadPG(t *testing.T, db *gorm.DB) s04PGDelta {
	t.Helper()
	var s s04PGDelta
	if err := db.Raw(`SELECT xact_commit,xact_rollback,blks_read,blks_hit,temp_bytes,deadlocks,tup_inserted,tup_updated,tup_deleted FROM pg_stat_database WHERE datname=current_database()`).
		Row().Scan(&s.XactCommit, &s.XactRollback, &s.BlksRead, &s.BlksHit, &s.TempBytes, &s.Deadlocks, &s.TupInserted, &s.TupUpdated, &s.TupDeleted); err != nil {
		t.Fatalf("pg_stat_database: %v", err)
	}
	return s
}

func (a s04PGDelta) minus(b s04PGDelta) s04PGDelta {
	return s04PGDelta{
		XactCommit: a.XactCommit - b.XactCommit, XactRollback: a.XactRollback - b.XactRollback,
		BlksRead: a.BlksRead - b.BlksRead, BlksHit: a.BlksHit - b.BlksHit,
		TempBytes: a.TempBytes - b.TempBytes, Deadlocks: a.Deadlocks - b.Deadlocks,
		TupInserted: a.TupInserted - b.TupInserted, TupUpdated: a.TupUpdated - b.TupUpdated,
		TupDeleted: a.TupDeleted - b.TupDeleted,
	}
}

type s04Result struct {
	Scenario       string            `json:"scenario"`
	StartedAt      string            `json:"started_at"`
	ElapsedSeconds float64           `json:"elapsed_seconds"`
	Parallelism    int               `json:"parallelism"`
	Items          int               `json:"items"`
	Succeeded      int64             `json:"succeeded"`
	PendingAfter   int64             `json:"pending_after"`
	Failures       int64             `json:"failures"`
	Throughput     float64           `json:"items_per_second"`
	PerItem        s04Latency        `json:"per_item_ms"`
	Batch          s04Latency        `json:"batch_ms"`
	Ticks          s04Latency        `json:"tick_ms"`
	TickCount      int               `json:"tick_count"`
	TD             s04Latency        `json:"td_ms"`
	TDFailed       int64             `json:"td_failed"`
	Snapshot       map[string]any    `json:"snapshot"`
	PGStatements   map[string]any    `json:"pg_statements"`
	PGDatabase     s04PGDelta        `json:"pg_database_delta"`
	ProcessCPU     float64           `json:"process_cpu_seconds"`
	MaxInUseConns  int               `json:"max_in_use_pg_conns"`
	QueueCurve     []map[string]any  `json:"queue_curve"`
	Counters       map[string]uint64 `json:"telemetry_counters"`
	Paths          map[string]any    `json:"telemetry_paths"`
	Notes          []string          `json:"notes"`
	Env            map[string]any    `json:"env"`
}

// ---------- 运行器 ----------

type s04ItemJob struct {
	item model.Inbox
	done chan struct{}
}

func s04DispatchItems(t *testing.T, s *Server, groups [][]model.Inbox, parallelism int, ctx context.Context) (perItem, batch []time.Duration, dispatched int) {
	t.Helper()
	jobs := make(chan s04ItemJob, parallelism)
	var wg sync.WaitGroup
	var mu sync.Mutex
	for n := 0; n < parallelism; n++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for job := range jobs {
				began := time.Now()
				s.processInboxItem(ctx, job.item)
				d := time.Since(began)
				mu.Lock()
				perItem = append(perItem, d)
				mu.Unlock()
				close(job.done)
			}
		}()
	}
	rounds := 0
	for _, g := range groups {
		rounds = max(rounds, len(g))
	}
	for r := 0; r < rounds; r++ {
		var round []s04ItemJob
		began := time.Now()
		for _, g := range groups {
			if r >= len(g) {
				continue
			}
			job := s04ItemJob{item: g[r], done: make(chan struct{})}
			round = append(round, job)
			jobs <- job
		}
		for _, job := range round {
			<-job.done
		}
		batch = append(batch, time.Since(began))
		dispatched += len(round)
	}
	close(jobs)
	wg.Wait()
	return perItem, batch, dispatched
}

type s04LoopOutcome struct {
	ticks        []time.Duration
	elapsed      time.Duration
	pendingAfter int64
	queue        []map[string]any
	maxInUse     int
}

func s04Pending(t *testing.T, db *gorm.DB, floor int64) int64 {
	t.Helper()
	var n int64
	if err := db.Raw("SELECT count(*) FROM telemetry_inbox WHERE processed_at IS NULL AND ts_ms>=?", floor).Scan(&n).Error; err != nil {
		t.Fatalf("pending count: %v", err)
	}
	return n
}

// s04RunLoop 复刻 server.worker 的 ticker(500ms) + processPendingBudget(inboxTickBudget)，
// 但把每次 tick 包起来计时；除此之外完全走真实调度入口。
func s04RunLoop(t *testing.T, s *Server, pool *sql.DB, parallelism int, limit time.Duration) s04LoopOutcome {
	t.Helper()
	floor := time.Now().UTC().Add(-inboxWindow).UnixMilli()
	s.inboxFloor.Store(floor)
	if err := s.db.Exec(`UPDATE telemetry_scheduler SET last_device_id=0,realtime_last_device_id=0,history_enabled=true,history_parallelism=? WHERE id=1`, parallelism).Error; err != nil {
		t.Fatalf("set scheduler: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), limit+30*time.Second)
	defer cancel()

	var maxInUse atomic.Int64
	stopSample := make(chan struct{})
	sampleDone := make(chan struct{})
	go func() {
		defer close(sampleDone)
		tk := time.NewTicker(100 * time.Millisecond)
		defer tk.Stop()
		for {
			select {
			case <-stopSample:
				return
			case <-tk.C:
				if v := int64(pool.Stats().InUse); v > maxInUse.Load() {
					maxInUse.Store(v)
				}
			}
		}
	}()

	out := s04LoopOutcome{}
	start := time.Now()
	ticker := time.NewTicker(500 * time.Millisecond)
	defer ticker.Stop()
	deadline := time.Now().Add(limit)
	for {
		select {
		case <-ctx.Done():
			out.elapsed = time.Since(start)
			out.pendingAfter = s04Pending(t, s.db, floor)
			close(stopSample)
			<-sampleDone
			out.maxInUse = int(maxInUse.Load())
			return out
		case <-ticker.C:
		}
		began := time.Now()
		s.processPendingBudget(ctx, inboxTickBudget)
		out.ticks = append(out.ticks, time.Since(began))
		pending := s04Pending(t, s.db, floor)
		out.queue = append(out.queue, map[string]any{"at_seconds": time.Since(start).Seconds(), "pending": pending})
		if pending == 0 {
			out.elapsed = time.Since(start)
			out.pendingAfter = 0
			close(stopSample)
			<-sampleDone
			out.maxInUse = int(maxInUse.Load())
			return out
		}
		if time.Now().After(deadline) {
			out.elapsed = time.Since(start)
			out.pendingAfter = pending
			close(stopSample)
			<-sampleDone
			out.maxInUse = int(maxInUse.Load())
			return out
		}
	}
}

func s04SuccessCount(t *testing.T, db *gorm.DB) int64 {
	t.Helper()
	var n int64
	if err := db.Raw(`SELECT count(*) FROM telemetry_inbox WHERE processed_at IS NOT NULL AND business_state='success'`).Scan(&n).Error; err != nil {
		t.Fatalf("success count: %v", err)
	}
	return n
}

func s04SnapshotRun(t *testing.T, s *Server, devices []model.Device, parallelism int) (perDevice []time.Duration, errs int) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 4*time.Minute)
	defer cancel()
	jobs := make(chan model.Device, parallelism)
	var wg sync.WaitGroup
	var mu sync.Mutex
	for n := 0; n < parallelism; n++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for d := range jobs {
				began := time.Now()
				err := s.refreshDeviceSnapshot(ctx, d)
				dur := time.Since(began)
				mu.Lock()
				perDevice = append(perDevice, dur)
				if err != nil {
					errs++
				}
				mu.Unlock()
			}
		}()
	}
	for _, d := range devices {
		jobs <- d
	}
	close(jobs)
	wg.Wait()
	return perDevice, errs
}

// s04MaxInUse 在 fn 执行期间采样连接池使用峰值。
func s04MaxInUse(pool *sql.DB, fn func()) int {
	var maxInUse atomic.Int64
	stop := make(chan struct{})
	done := make(chan struct{})
	go func() {
		defer close(done)
		tk := time.NewTicker(50 * time.Millisecond)
		defer tk.Stop()
		for {
			select {
			case <-stop:
				return
			case <-tk.C:
				if v := int64(pool.Stats().InUse); v > maxInUse.Load() {
					maxInUse.Store(v)
				}
			}
		}
	}()
	fn()
	close(stop)
	<-done
	if v := int64(pool.Stats().InUse); v > maxInUse.Load() {
		maxInUse.Store(v)
	}
	return int(maxInUse.Load())
}

// ---------- 主测试 ----------

func TestIRS04CapacityIsolated(t *testing.T) {
	env := s04RequireFixture(t)
	scenario := s04EnvOf("IR_S04_SCENARIO", "item-backlog-p8")
	outDir := s04EnvOf("IR_S04_OUT", filepath.Join("..", "..", "..", "..", "artifacts", "ir-s04", "2026-11-06"))

	// 场景参数（固定输入的一部分，结果里原样记录）。
	rowsPerDevice := 20
	parallelism := 8
	s02On := false
	s03On := false
	tdMiss := false
	mode := "item" // item | loop | fresh
	useCounter := true
	sweepOn := false
	switch scenario {
	case "loop-backlog-p8":
		mode, useCounter = "loop", false
	case "loop-backlog-p8-sweep":
		mode, useCounter, sweepOn = "loop", false, true
	case "loop-backlog-p8-s02on":
		mode, useCounter, s02On = "loop", false, true
	case "loop-backlog-p8-s02s03on":
		mode, useCounter, s02On, s03On = "loop", false, true, true
	case "item-backlog-p8":
	case "item-backlog-p8-silent":
		useCounter = false
	case "item-backlog-p4":
		rowsPerDevice, parallelism = 10, 4
	case "item-backlog-p1":
		rowsPerDevice, parallelism = 1, 1
	case "item-backlog-p8-s03on":
		rowsPerDevice, s03On = 10, true
	case "item-fresh-p8":
		mode, rowsPerDevice = "fresh", 2
	case "item-tdmiss-p8-s03off":
		rowsPerDevice, tdMiss = 1, true
	case "item-tdmiss-p8-s03on":
		rowsPerDevice, tdMiss, s03On = 1, true, true
	default:
		t.Fatalf("unknown IR_S04_SCENARIO %q", scenario)
	}

	counter := newS04QueryCounter()
	var gormLogger logger.Interface = logger.Default.LogMode(logger.Silent)
	if useCounter {
		gormLogger = counter
	}
	db, pool := s04Open(t, env.dsn, gormLogger)
	defer pool.Close()
	s04ResetSchema(t, db)

	specs := s04FleetSpecs(t)
	devices := s04SeedDevices(t, db, specs)

	rd := redis.NewClient(&redis.Options{Addr: env.redis, ContextTimeoutEnabled: true, MaxRetries: -1})
	defer rd.Close()
	if err := rd.Ping(context.Background()).Err(); err != nil {
		t.Fatalf("fixture Redis: %v", err)
	}
	if err := rd.FlushDB(context.Background()).Err(); err != nil {
		t.Fatalf("flush fixture Redis: %v", err)
	}

	proxy, tdStats := s04StartTDProxy(t, env.td)
	cfg := config.Config{
		TDURL: proxy.URL, TDUser: "root", TDPassword: "taosdata", TDDatabase: s04FixtureTDDb,
		JWTSecret: strings.Repeat("x", 32), SimulationAlarmMode: "off",
		FleetPath:                  "../../../01-data-chain/simulator/fleet.json",
		InboxExpiryEnabled:         s02On,
		InboxExpiryBatch:           2000,
		InboxExpiryIntervalSeconds: 30,
		InboxExpiryBudgetSeconds:   5,
		TDMissingCircuitEnabled:    s03On,
	}
	td := tdengine.New(cfg)
	srv := New(cfg, db, rd, td, zap.NewNop())
	t.Cleanup(func() {
		if srv.realtime.cancel != nil {
			srv.realtime.cancel()
			<-srv.realtime.done
		}
	})

	// 输入时钟：历史积压取 2 小时前接收且 ts 同步后移；新鲜类取当前时刻。
	now := time.Now().UTC()
	tsBase := now.Add(-2 * time.Hour)
	receivedAt := now.Add(-2 * time.Hour)
	if mode == "fresh" {
		tsBase = now.Add(-5 * time.Second)
		receivedAt = now
	}

	tdSeqShift := int64(0)
	if tdMiss {
		tdSeqShift = 1000
	}
	s04SeedTD(t, td, devices, scenario, rowsPerDevice, tdSeqShift, tsBase)
	groups := s04SeedInbox(t, db, devices, scenario, rowsPerDevice, tsBase, receivedAt)

	items := 0
	for _, g := range groups {
		items += len(g)
	}

	// 统计基线（夹具/种子不计入）。
	tdStats.reset()
	counter.Reset()
	pgBefore := s04ReadPG(t, db)
	cpuBefore := s04CPU()
	successBefore := s04SuccessCount(t, db)
	floor := now.Add(-inboxWindow).UnixMilli()
	pendingBefore := s04Pending(t, db, floor)

	var (
		perItem, batch, ticks []time.Duration
		elapsed               time.Duration
		dispatched            int
		pendingAfter          int64
		maxInUse              int
		queueCurve            []map[string]any
		snapshotInfo          map[string]any
		stmtSnapshot          map[string]any
	)

	switch mode {
	case "loop":
		var workerCtx context.Context
		var stopWorker context.CancelFunc
		if s02On || sweepOn {
			workerCtx, stopWorker = context.WithCancel(context.Background())
			defer stopWorker()
		}
		if s02On {
			go srv.expiryWorker(workerCtx)
		}
		if sweepOn {
			go srv.snapshotWorker(workerCtx)
		}
		out := s04RunLoop(t, srv, pool, parallelism, 10*time.Minute)
		ticks, elapsed, pendingAfter, maxInUse, queueCurve = out.ticks, out.elapsed, out.pendingAfter, out.maxInUse, out.queue
	case "item", "fresh":
		srv.inboxFloor.Store(floor)
		if err := srv.db.Exec(`UPDATE telemetry_scheduler SET history_enabled=true,history_parallelism=? WHERE id=1`, parallelism).Error; err != nil {
			t.Fatalf("set scheduler: %v", err)
		}
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Minute)
		began := time.Now()
		maxInUse = s04MaxInUse(pool, func() {
			perItem, batch, dispatched = s04DispatchItems(t, srv, groups, parallelism, ctx)
		})
		elapsed = time.Since(began)
		cancel()
		if useCounter {
			stmtSnapshot = counter.Snapshot(items)
		}
		pendingAfter = s04Pending(t, db, floor)
		if mode == "fresh" {
			snapshotInfo = s04MeasureSnapshot(t, srv, devices, rd, pool)
		}
	}

	pgAfter := s04ReadPG(t, db)
	cpuAfter := s04CPU()
	tdLat, tdFailed := tdStats.reset()
	successAfter := s04SuccessCount(t, db)

	res := s04Result{
		Scenario: scenario, StartedAt: now.Format(time.RFC3339), ElapsedSeconds: elapsed.Seconds(),
		Parallelism: parallelism, Items: items, Succeeded: successAfter - successBefore, PendingAfter: pendingAfter,
		TickCount: len(ticks), TDFailed: tdFailed, Snapshot: snapshotInfo, PGStatements: stmtSnapshot,
		PGDatabase: pgAfter.minus(pgBefore), ProcessCPU: cpuAfter - cpuBefore, MaxInUseConns: maxInUse,
		QueueCurve: queueCurve, Counters: srv.telemetryCounters(), Paths: srv.telemetry.durations(),
		Env: map[string]any{
			"fleet_devices": len(devices), "rows_per_device": rowsPerDevice,
			"received_at": receivedAt.Format(time.RFC3339), "ts_base": tsBase.Format(time.RFC3339),
			"td_seq_shift": tdSeqShift, "s02_explicit_on": s02On, "s03_explicit_on": s03On,
			"snapshot_worker_on": sweepOn,
			"mode":               mode, "sql_counter": useCounter, "pending_before": pendingBefore,
			"pg_pool_max": s04PoolMax, "gomaxprocs": runtime.GOMAXPROCS(0),
		},
	}
	res.Failures = int64(dispatched) - res.Succeeded
	if res.Failures < 0 {
		res.Failures = 0
	}
	if elapsed > 0 {
		res.Throughput = float64(res.Succeeded) / elapsed.Seconds()
	}
	if res.PGStatements == nil {
		res.PGStatements = map[string]any{"disabled": true}
	}
	res.PerItem = s04LatencyOf(perItem)
	res.Batch = s04LatencyOf(batch)
	res.Ticks = s04LatencyOf(ticks)
	res.TD = s04LatencyOf(tdLat)
	res.Notes = []string{
		"per-item 由测试包裹真实 processInboxItem 计时；SQL 计数场景会加少量 GORM Trace 开销，另有 -silent 对照。",
		"batch 为一轮按 history_parallelism 派发的同设备队头组耗时；tick 为真实 processPendingBudget 每次调用耗时。",
		"P99 用 nearest-rank；n<100 时 p99 等于样本最大值。",
		"TD 延迟经 loopback 反向代理记录，包含代理转发开销；命中/查无由真实 TDengine 3.4.2.8 返回。",
		"未启动 energy 重建 worker、告警投递 worker、HTTP 业务流量；未复现生产 143 台/5 秒实时 ingest 与 production 磁盘差异。",
	}

	if err := os.MkdirAll(outDir, 0o755); err != nil {
		t.Logf("mkdir %s: %v", outDir, err)
	} else {
		path := filepath.Join(outDir, scenario+".json")
		if b, err := json.MarshalIndent(res, "", "  "); err == nil {
			if err := os.WriteFile(path, b, 0o644); err != nil {
				t.Logf("write %s: %v", path, err)
			} else {
				t.Logf("IR-S04 result written: %s", path)
			}
		}
	}

	t.Logf("SCENARIO %s items=%d success=%d pending=%d elapsed=%.3fs throughput=%.2f/s per_item p50=%.2f p95=%.2f p99=%.2f ms td p50=%.2f p95=%.2f p99=%.2f ms cpu=%.2fs",
		scenario, res.Items, res.Succeeded, res.PendingAfter, res.ElapsedSeconds, res.Throughput,
		res.PerItem.P50MS, res.PerItem.P95MS, res.PerItem.P99MS, res.TD.P50MS, res.TD.P95MS, res.TD.P99MS, res.ProcessCPU)

	if mode == "item" || mode == "fresh" {
		if !tdMiss && res.Succeeded != int64(res.Items) {
			t.Fatalf("scenario %s: only %d/%d items succeeded (pending=%d)", scenario, res.Succeeded, res.Items, res.PendingAfter)
		}
	}
	if tdMiss && res.Succeeded != 0 {
		t.Fatalf("tdmiss scenario must not succeed, got %d", res.Succeeded)
	}
	if mode == "loop" && pendingAfter != 0 {
		t.Fatalf("loop scenario did not drain: pending=%d", pendingAfter)
	}
}

// s04MeasureSnapshot 在新鲜输入处理完之后量 Redis 结果写入：
//   - cold：清空 fixture Redis，逐设备 refreshDeviceSnapshot（走 CAS 写事务）；
//   - warm：不清空，逐设备再走一次（命中 fast-path 只读比较）；
//   - sweep：清空后跑一次真实 snapshotSweep（128/页 + 回绕页 + 游标更新）。
func s04MeasureSnapshot(t *testing.T, s *Server, devices []model.Device, rd *redis.Client, pool *sql.DB) map[string]any {
	t.Helper()
	info := map[string]any{}
	ctx := context.Background()

	if err := rd.FlushDB(ctx).Err(); err != nil {
		t.Fatalf("flush redis: %v", err)
	}
	writesBefore := s.telemetryCounters()["redis_write"]
	var coldMax int
	var cold []time.Duration
	var coldErrs int
	coldMax = s04MaxInUse(pool, func() { cold, coldErrs = s04SnapshotRun(t, s, devices, 8) })
	info["cold_per_device_ms"] = s04LatencyOf(cold)
	info["cold_errors"] = coldErrs
	info["cold_redis_writes"] = s.telemetryCounters()["redis_write"] - writesBefore
	info["cold_max_in_use_conns"] = coldMax

	warmBeganWrites := s.telemetryCounters()["redis_write"]
	warm, warmErrs := s04SnapshotRun(t, s, devices, 8)
	info["warm_per_device_ms"] = s04LatencyOf(warm)
	info["warm_errors"] = warmErrs
	info["warm_redis_writes"] = s.telemetryCounters()["redis_write"] - warmBeganWrites

	if err := rd.FlushDB(ctx).Err(); err != nil {
		t.Fatalf("flush redis: %v", err)
	}
	if err := s.db.Exec(`UPDATE telemetry_scheduler SET realtime_last_device_id=0 WHERE id=1`).Error; err != nil {
		t.Fatalf("reset snapshot cursor: %v", err)
	}
	sweepWritesBefore := s.telemetryCounters()["redis_write"]
	// 一次 sweep 只处理一页（128 台）；再跑一次覆盖回绕页的 15 台。
	var sweeps []time.Duration
	for pass := 0; pass < 2; pass++ {
		began := time.Now()
		if err := s.snapshotSweep(ctx); err != nil {
			t.Logf("snapshotSweep pass %d: %v", pass+1, err)
		}
		sweeps = append(sweeps, time.Since(began))
	}
	info["sweep_pass_ms"] = s04LatencyOf(sweeps)
	info["sweep_devices"] = 143
	info["sweep_redis_writes"] = s.telemetryCounters()["redis_write"] - sweepWritesBefore

	keys := 0
	for _, d := range devices {
		if rd.Exists(ctx, fmt.Sprintf("device:%d:latest", d.ID)).Val() == 1 {
			keys++
		}
	}
	info["redis_device_keys"] = keys
	return info
}

// ============================================================================
// IR-S04 第二轮：混合负载基线
//
// 与首轮的区别（首轮只测历史 worker 的隔离上限）：
//   - 输入走真实 API 接收管线：acceptTelemetry -> enqueueAdmission(QoS1 顺序 ACK)
//     -> 4 个 receive 协程 -> 真实 inbox INSERT（含 device 查询、维护闸门、
//     设备 advisory lock、energy_dirty_days）。未启动真实 MQTT broker / 独立
//     gateway 进程，TD 行由夹具预写；该缺口在结果 notes 里明确写出。
//   - 同时启动真实 snapshotWorker（TD 实时确认 + telemetry_snapshots + Redis CAS）
//     与 energyWorker（日聚合重建），并可选 S02 expiryWorker。
//   - 历史调度循环是 processPendingBudget 的逐行镜像，唯一增加的是在
//     processInboxItem 调用点计时；SQL、队头选择、游标推进、8 路 worker 与
//     500ms ticker 都保持生产语义。
//   - 固定输入是「历史积压 + 实时流」；实时的 ts 由夹具预先排定并写入 TD，
//     生产者按墙钟发送，经真实 API 接收路径落库。
//   - 每组默认 3 轮，每轮重建 schema/TD/Redis，P50/P95/P99 用 nearest-rank 并给出 n。
//
// 这不是生产压测，也不接触生产、home-dev 业务库、owner_abandoned、TD/energy 历史。
// ============================================================================

const s04MixedDefaultOut = "2026-11-07"

type s04MixedSpec struct {
	Name             string
	FleetSize        int
	BacklogPerDevice int
	Rate             float64
	Duration         time.Duration
	Parallelism      int
	S02              bool
	S03              bool
	Drain            time.Duration
	Counted          bool
	RealWorker       bool
	Capacity         bool
	TDWriteDelay     time.Duration
}

func s04MixedSpecOf(name string) (s04MixedSpec, error) {
	base := s04MixedSpec{Name: name, FleetSize: 143, Parallelism: 8, Drain: 10 * time.Second}
	switch name {
	case "mixed-capacity-143":
		base.BacklogPerDevice, base.Rate, base.Duration, base.Drain, base.Capacity = 40, 24, 20*time.Second, 5*time.Second, true
	case "mixed-capacity-143-realworker":
		base.BacklogPerDevice, base.Rate, base.Duration, base.Drain, base.Capacity, base.RealWorker = 40, 24, 20*time.Second, 5*time.Second, true, true
	case "mixed-capacity-200":
		base.FleetSize, base.BacklogPerDevice, base.Rate, base.Duration, base.Drain, base.Capacity = 200, 30, 24, 20*time.Second, 5*time.Second, true
	case "mixed-steady-143-24":
		base.Rate, base.Duration = 24, 60*time.Second
	case "mixed-steady-143-24-counted":
		base.Rate, base.Duration, base.Drain, base.Counted = 24, 30*time.Second, 5*time.Second, true
	case "mixed-steady-143-48":
		base.Rate, base.Duration = 48, 60*time.Second
	case "mixed-steady-143-48-s02s03":
		base.Rate, base.Duration, base.S02, base.S03 = 48, 60*time.Second, true, true
	case "mixed-steady-200-24":
		base.FleetSize, base.Rate, base.Duration = 200, 24, 60*time.Second
	case "mixed-delay-143-24":
		base.Rate, base.Duration, base.Drain, base.TDWriteDelay = 24, 30*time.Second, 10*time.Second, 800*time.Millisecond
	case "mixed-delay-backlog-143-24":
		base.BacklogPerDevice, base.Rate, base.Duration, base.Drain, base.TDWriteDelay = 3, 24, 30*time.Second, 10*time.Second, 800*time.Millisecond
	case "mixed-delay-backlog-143-96-s02":
		base.BacklogPerDevice, base.Rate, base.Duration, base.Drain, base.TDWriteDelay, base.S02 = 3, 96, 30*time.Second, 10*time.Second, 800*time.Millisecond, true
	case "mixed-delay-backlog-143-48-s02":
		base.BacklogPerDevice, base.Rate, base.Duration, base.Drain, base.TDWriteDelay, base.S02 = 3, 48, 30*time.Second, 10*time.Second, 800*time.Millisecond, true
	case "mixed-delay-backlog-143-72-s02":
		base.BacklogPerDevice, base.Rate, base.Duration, base.Drain, base.TDWriteDelay, base.S02 = 3, 72, 30*time.Second, 10*time.Second, 800*time.Millisecond, true
	case "mixed-delay-backlog-143-144-s02":
		base.BacklogPerDevice, base.Rate, base.Duration, base.Drain, base.TDWriteDelay, base.S02 = 3, 144, 30*time.Second, 10*time.Second, 800*time.Millisecond, true
	default:
		return base, fmt.Errorf("unknown IR_S04 mixed scenario %q", name)
	}
	if base.FleetSize != 143 && base.FleetSize != 200 {
		return base, fmt.Errorf("IR-S04 mixed fleet must be 143 or 200, got %d", base.FleetSize)
	}
	if base.Rate <= 0 || base.Duration <= 0 {
		return base, fmt.Errorf("invalid rate/duration")
	}
	return base, nil
}

func s04EnvInt(key string, fallback int) int {
	v, err := strconv.Atoi(s04EnvOf(key, ""))
	if err != nil || v < 1 {
		return fallback
	}
	return v
}

// s04MSLatency 与 s04Latency 同口径，但保留亚毫秒样本（接收至成功等由 SQL
// EXTRACT(EPOCH)*1000 得到，直接是浮点毫秒）。
type s04MSLatency struct {
	N      int     `json:"n"`
	MinMS  float64 `json:"min_ms"`
	P50MS  float64 `json:"p50_ms"`
	P95MS  float64 `json:"p95_ms"`
	P99MS  float64 `json:"p99_ms"`
	MeanMS float64 `json:"mean_ms"`
	MaxMS  float64 `json:"max_ms"`
}

func s04LatencyOfMS(in []float64) s04MSLatency {
	out := s04MSLatency{N: len(in)}
	if len(in) == 0 {
		return out
	}
	xs := append([]float64(nil), in...)
	sort.Float64s(xs)
	var sum float64
	for _, v := range xs {
		sum += v
	}
	at := func(p float64) float64 {
		idx := int(math.Ceil(p*float64(len(xs)))) - 1
		if idx < 0 {
			idx = 0
		}
		if idx >= len(xs) {
			idx = len(xs) - 1
		}
		return xs[idx]
	}
	out.MinMS, out.P50MS, out.P95MS, out.P99MS, out.MaxMS = xs[0], at(0.50), at(0.95), at(0.99), xs[len(xs)-1]
	out.MeanMS = sum / float64(len(xs))
	return out
}

// s04Message 是 paho.MQTT Message 的测试替身。Ack 由真实 ackAdmissions 在
// durable 之后调用，用来量「发射 -> enqueue -> 持久 -> ACK」完整链路。
type s04Message struct {
	payload    []byte
	id         uint16
	target     time.Time
	enqueuedAt atomic.Int64
	ackAt      atomic.Int64
}

func (m *s04Message) Duplicate() bool   { return false }
func (m *s04Message) Qos() byte         { return 1 }
func (m *s04Message) Retained() bool    { return false }
func (m *s04Message) Topic() string     { return "device/telemetry" }
func (m *s04Message) MessageID() uint16 { return m.id }
func (m *s04Message) Payload() []byte   { return m.payload }
func (m *s04Message) Ack()              { m.ackAt.CompareAndSwap(0, time.Now().UnixNano()) }

// s04LogCounter 是 zapcore.Core 的计数实现。业务代码的告警/错误本来就是
// 通过日志暴露的（telemetryLog、energy_rebuild_*、snapshot_sweep），这里不
// 改日志调用，只在测试进程内按 message(+reason) 计数，作为错误/重试证据。
type s04LogCounter struct {
	mu     sync.Mutex
	counts map[string]int64
}

func newS04LogCounter() *s04LogCounter {
	return &s04LogCounter{counts: map[string]int64{}}
}

func (c *s04LogCounter) Enabled(zapcore.Level) bool        { return true }
func (c *s04LogCounter) With([]zapcore.Field) zapcore.Core { return c }
func (c *s04LogCounter) Sync() error                       { return nil }
func (c *s04LogCounter) Check(ent zapcore.Entry, ce *zapcore.CheckedEntry) *zapcore.CheckedEntry {
	return ce.AddCore(ent, c)
}
func (c *s04LogCounter) Write(ent zapcore.Entry, fields []zapcore.Field) error {
	key := ent.Message
	for _, f := range fields {
		if f.Key == "reason" && f.Type == zapcore.StringType {
			key += ":" + f.String
		}
	}
	c.mu.Lock()
	c.counts[key]++
	c.mu.Unlock()
	return nil
}
func (c *s04LogCounter) reset() {
	c.mu.Lock()
	c.counts = map[string]int64{}
	c.mu.Unlock()
}
func (c *s04LogCounter) snapshot() map[string]int64 {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := make(map[string]int64, len(c.counts))
	for k, v := range c.counts {
		out[k] = v
	}
	return out
}

// s04CgroupCPU 读取一次性依赖容器的 cgroup v2 usage_usec。只接受本夹具的
// 容器名；失败返回 ok=false，由结果标注不可用，不阻塞测试。
func s04CgroupCPU(container string) (float64, bool) {
	if !strings.HasPrefix(container, "ir-s04-") {
		return 0, false
	}
	out, err := exec.Command("docker", "exec", container, "cat", "/sys/fs/cgroup/cpu.stat").Output()
	if err != nil {
		return 0, false
	}
	for _, line := range strings.Split(string(out), "\n") {
		line = strings.TrimSpace(line)
		if !strings.HasPrefix(line, "usage_usec ") {
			continue
		}
		usec, err := strconv.ParseFloat(strings.TrimSpace(strings.TrimPrefix(line, "usage_usec ")), 64)
		if err != nil {
			return 0, false
		}
		return usec / 1e6, true
	}
	return 0, false
}

func s04MixedFleetSpecs(t *testing.T, size int) []s04DeviceSpec {
	t.Helper()
	specs := s04FleetSpecs(t)
	if len(specs) > size {
		t.Fatalf("IR-S04 mixed fleet size %d smaller than the 143-device manifest", size)
	}
	for i := len(specs); i < size; i++ {
		specs = append(specs, s04DeviceSpec{
			Code:    fmt.Sprintf("ST-LOAD-INV-%02d", i-143+1),
			Station: "ST-LOAD",
		})
	}
	return specs
}

// ---------- 固定输入排程与一次性 TD ----------

type s04Planned struct {
	device model.Device
	sample telemetry.Sample
	msg    *s04Message
}

func s04PlanFresh(devices []model.Device, runID string, rate float64, duration time.Duration, start time.Time) ([]*s04Planned, map[int64][]telemetry.Sample, int, error) {
	n := int(math.Round(rate * duration.Seconds()))
	if n < 1 {
		return nil, nil, 0, fmt.Errorf("empty input plan")
	}
	period := time.Duration(float64(time.Second) / rate)
	out := make([]*s04Planned, 0, n)
	byDevice := map[int64][]telemetry.Sample{}
	for i := 0; i < n; i++ {
		d := devices[i%len(devices)]
		seq := int64(i/len(devices)) + 1
		at := start.Add(time.Duration(i) * period)
		s := s04Sample(runID, d, seq, at.UnixMilli())
		payload, err := json.Marshal(s)
		if err != nil {
			return nil, nil, 0, err
		}
		out = append(out, &s04Planned{
			device: d,
			sample: s,
			msg: &s04Message{
				payload: payload,
				id:      uint16(i % 65535),
				target:  at,
			},
		})
		byDevice[d.ID] = append(byDevice[d.ID], s)
	}
	return out, byDevice, n, nil
}

// s04ResetTD 每轮重建一次性 TD 库，避免上一轮同 ts/seq 的行把本轮 HasSample
// 伪造成命中。只操作一次性容器里的 ir_s04 库。
func s04ResetTD(t *testing.T, td *tdengine.Client) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Minute)
	defer cancel()
	var last error
	for attempt := 0; attempt < 20; attempt++ {
		if _, err := td.SQL(ctx, "DROP DATABASE IF EXISTS "+s04FixtureTDDb); err == nil {
			last = nil
			break
		} else {
			last = err
			time.Sleep(300 * time.Millisecond)
		}
	}
	if last != nil {
		t.Fatalf("drop fixture TD database: %v", last)
	}
	for attempt := 0; attempt < 20; attempt++ {
		if _, err := td.SQL(ctx, "CREATE DATABASE IF NOT EXISTS "+s04FixtureTDDb+" PRECISION 'ms' KEEP 365 DURATION 10"); err == nil {
			last = nil
			break
		} else {
			last = err
			time.Sleep(300 * time.Millisecond)
		}
	}
	if last != nil {
		t.Fatalf("create fixture TD database: %v", last)
	}
	if _, err := td.SQL(ctx, "CREATE STABLE IF NOT EXISTS "+s04FixtureTDDb+".telemetry (ts TIMESTAMP, seq BIGINT, voltage FLOAT, current FLOAT, temperature FLOAT, power FLOAT, status INT, fault_code INT) TAGS (device_id BINARY(64), station_id BINARY(64))"); err != nil {
		t.Fatalf("create fixture TD stable: %v", err)
	}
}

func s04SeedBacklogTD(t *testing.T, td *tdengine.Client, devices []model.Device, runID string, per int, base time.Time) int {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 4*time.Minute)
	defer cancel()
	total := 0
	for _, d := range devices {
		samples := make([]telemetry.Sample, 0, per)
		for i := 0; i < per; i++ {
			samples = append(samples, s04Sample(runID, d, int64(i+1), base.Add(time.Duration(i)*5*time.Second).UnixMilli()))
		}
		if err := td.Insert(ctx, d, samples); err != nil {
			t.Fatalf("seed backlog TD device %d: %v", d.ID, err)
		}
		total += len(samples)
	}
	return total
}

func s04SeedFreshTD(t *testing.T, td *tdengine.Client, devices []model.Device, byDevice map[int64][]telemetry.Sample) int {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 4*time.Minute)
	defer cancel()
	total := 0
	for _, d := range devices {
		samples := byDevice[d.ID]
		if len(samples) == 0 {
			continue
		}
		if err := td.Insert(ctx, d, samples); err != nil {
			t.Fatalf("seed fresh TD device %d: %v", d.ID, err)
		}
		total += len(samples)
	}
	return total
}

func s04EnsureFixtureTDTables(t *testing.T, td *tdengine.Client, devices []model.Device) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	for _, d := range devices {
		if err := td.EnsureDevice(ctx, d); err != nil {
			t.Fatalf("ensure empty fixture TD table for device %d: %v", d.ID, err)
		}
	}
}

// ---------- 真实 API 接收管线（无 MQTT broker） ----------

// s04StartIngress 复刻 server.subscribe 的内部接收管线：1 个有序 ACK 协程 +
// 4 个 receive 协程 + 128 槽准入缓冲。生产者调用真实的 acceptTelemetry，
// 因此 device 查询、维护闸门、设备 advisory lock、inbox INSERT、energy_dirty
// 都是生产 SQL；唯一缺的是 MQTT broker/独立 gateway 进程。
func s04StartIngress(ctx context.Context, s *Server) func() {
	s.ingressQueue = make(chan mqtt.Message, 128)
	s.ingressAcks = make(chan *orderedAdmission, 128)
	s.ingressSlots = make(chan struct{}, 128)
	s.ingressDone = make(chan struct{})
	var receivers sync.WaitGroup
	receivers.Add(1)
	go func() { defer receivers.Done(); s.ackAdmissions(ctx) }()
	for n := 0; n < 4; n++ {
		receivers.Add(1)
		go func() {
			defer receivers.Done()
			for {
				select {
				case <-ctx.Done():
					return
				case message := <-s.ingressQueue:
					for !s.receive(nil, message) {
						timer := time.NewTimer(time.Second)
						select {
						case <-ctx.Done():
							timer.Stop()
							return
						case <-timer.C:
						}
					}
				}
			}
		}()
	}
	go func() { receivers.Wait(); close(s.ingressDone) }()
	return func() {
		select {
		case <-s.ingressDone:
		case <-time.After(10 * time.Second):
		}
	}
}

type s04ProducerStats struct {
	mu           sync.Mutex
	sent         int
	slowEnqueues int
	maxLagMS     float64
	maxEnqueueMS float64
}

func (p *s04ProducerStats) observe(lag, enqueue time.Duration) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.sent++
	if lag > 0 {
		if ms := float64(lag.Microseconds()) / 1000; ms > p.maxLagMS {
			p.maxLagMS = ms
		}
	}
	if ms := float64(enqueue.Microseconds()) / 1000; ms > p.maxEnqueueMS {
		p.maxEnqueueMS = ms
	}
	if enqueue > 100*time.Millisecond {
		p.slowEnqueues++
	}
}

func (p *s04ProducerStats) snapshot() (sent, slow int, maxLagMS, maxEnqueueMS float64) {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.sent, p.slowEnqueues, p.maxLagMS, p.maxEnqueueMS
}

func s04RunProducer(s *Server, plan []*s04Planned, stop <-chan struct{}, st *s04ProducerStats) {
	for _, p := range plan {
		select {
		case <-stop:
			return
		default:
		}
		if d := time.Until(p.msg.target); d > 0 {
			time.Sleep(d)
		}
		lag := time.Since(p.msg.target)
		began := time.Now()
		s.acceptTelemetry(nil, p.msg)
		enqueue := time.Since(began)
		p.msg.enqueuedAt.Store(time.Now().UnixNano())
		st.observe(lag, enqueue)
	}
}

// s04RunDelayedTD writes only to the guarded, one-off TD fixture. It uses a
// separate client so the query proxy continues to measure HasSample calls.
// The recorded lag is from planned API emission to successful TD INSERT return:
// an upper-bound proxy for visibility, not a gateway/MQTT latency measurement.
type s04TDWriterStats struct {
	mu      sync.Mutex
	written int
	errors  int
	lagsMS  []float64
}

func (st *s04TDWriterStats) snapshot() (int, int, []float64) {
	st.mu.Lock()
	defer st.mu.Unlock()
	return st.written, st.errors, append([]float64(nil), st.lagsMS...)
}

func s04RunDelayedTD(ctx context.Context, td *tdengine.Client, plan []*s04Planned, delay time.Duration, st *s04TDWriterStats) {
	for _, p := range plan {
		at := p.msg.target.Add(delay)
		if wait := time.Until(at); wait > 0 {
			timer := time.NewTimer(wait)
			select {
			case <-ctx.Done():
				timer.Stop()
				return
			case <-timer.C:
			}
		}
		var err error
		for attempt := 0; attempt < 3; attempt++ {
			dep, cancel := context.WithTimeout(ctx, 2*time.Second)
			err = td.Insert(dep, p.device, []telemetry.Sample{p.sample})
			cancel()
			if err == nil || ctx.Err() != nil {
				break
			}
			time.Sleep(100 * time.Millisecond)
		}
		st.mu.Lock()
		if err == nil {
			st.written++
			st.lagsMS = append(st.lagsMS, float64(time.Since(p.msg.target).Microseconds())/1000)
		} else {
			st.errors++
		}
		st.mu.Unlock()
		if ctx.Err() != nil {
			return
		}
	}
}

// ---------- 历史调度循环（processPendingBudget 的逐行镜像） ----------

type s04LoopStats struct {
	mu      sync.Mutex
	perItem []time.Duration
	ticks   []time.Duration
	heads   []int
	visits  int
}

func (s *s04LoopStats) addItem(d time.Duration) {
	s.mu.Lock()
	s.perItem = append(s.perItem, d)
	s.mu.Unlock()
}
func (s *s04LoopStats) addTick(d time.Duration, heads int) {
	s.mu.Lock()
	s.ticks = append(s.ticks, d)
	s.heads = append(s.heads, heads)
	s.mu.Unlock()
}
func (s *s04LoopStats) visit(n int) {
	s.mu.Lock()
	s.visits += n
	s.mu.Unlock()
}
func (s *s04LoopStats) snapshot() (perItem, ticks []time.Duration, heads []int, visits int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]time.Duration(nil), s.perItem...), append([]time.Duration(nil), s.ticks...), append([]int(nil), s.heads...), s.visits
}

func s04MixedLoop(ctx context.Context, s *Server, stop <-chan struct{}, st *s04LoopStats) {
	ticker := time.NewTicker(500 * time.Millisecond)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-stop:
			return
		case <-ticker.C:
		}
		began := time.Now()
		heads := s.s04MixedTick(ctx, st)
		st.addTick(time.Since(began), heads)
	}
}

// s04MixedTick 与 server.processPendingBudget 的第 129—204 行一一对应；
// 唯一差别是在 processInboxItem 调用点加了 per-item 计时。
func (s *Server) s04MixedTick(parent context.Context, st *s04LoopStats) int {
	s.scheduler.mu.Lock()
	defer s.scheduler.mu.Unlock()
	s.telemetry.touch("loop", time.Now())
	ctx, cancel := context.WithTimeout(parent, inboxTickBudget)
	defer cancel()
	if s.alarmMaintenance(ctx) {
		return 0
	}
	var cursor struct {
		LastDeviceID       int64
		HistoryEnabled     bool
		HistoryParallelism int
	}
	r := s.db.WithContext(ctx).Raw(`SELECT last_device_id,history_enabled,history_parallelism FROM telemetry_scheduler WHERE id=1`).Scan(&cursor)
	if r.Error != nil || r.RowsAffected != 1 {
		s.telemetryLog("scheduler_read", time.Now())
		return 0
	}
	if !cursor.HistoryEnabled || cursor.HistoryParallelism < 1 || cursor.HistoryParallelism > 8 {
		return 0
	}
	floor := time.Now().UTC().Add(-inboxWindow).UnixMilli()
	s.inboxFloor.Store(floor)
	start := cursor.LastDeviceID
	items, err := s.inboxHeads(ctx, start, floor, 0, inboxPageSize)
	if err != nil {
		s.telemetryLog("inbox_read", time.Now())
		return 0
	}
	if len(items) < inboxPageSize && start > 0 {
		tail, e := s.inboxHeads(ctx, 0, floor, start, inboxPageSize-len(items))
		if e != nil {
			return len(items)
		}
		items = append(items, tail...)
		s.telemetry.touch("scheduler_wrap", time.Now())
	}
	heads := len(items)
	jobs := make(chan model.Inbox, cursor.HistoryParallelism)
	var workers sync.WaitGroup
	for n := 0; n < cursor.HistoryParallelism; n++ {
		workers.Add(1)
		go func() {
			defer workers.Done()
			for item := range jobs {
				if ctx.Err() == nil {
					began := time.Now()
					s.processInboxItem(ctx, item)
					st.addItem(time.Since(began))
				}
			}
		}()
	}
	defer func() { close(jobs); workers.Wait() }()
	for begin := 0; begin < len(items); begin += cursor.HistoryParallelism {
		if ctx.Err() != nil {
			s.telemetry.touch("scheduler_budget", time.Now())
			return heads
		}
		group := items[begin:min(begin+cursor.HistoryParallelism, len(items))]
		if e := s.advanceTelemetryCursor(ctx, "last_device_id", group[len(group)-1].DeviceID); e != nil {
			return heads
		}
		for _, item := range group {
			s.telemetry.touch("scheduler_visit", time.Now())
			st.visit(1)
			if item.NextAttemptAt != nil && item.NextAttemptAt.After(time.Now()) {
				continue
			}
			select {
			case jobs <- item:
			case <-ctx.Done():
				return heads
			}
		}
	}
	return heads
}

// ---------- 队列采样与 SQL 统计 ----------

type s04PendingSample struct {
	AtS     float64 `json:"at_s"`
	Fresh   int64   `json:"fresh"`
	Backlog int64   `json:"backlog"`
}

func s04PendingPair(db *gorm.DB, freshRun, backlogRun string, floor int64) (int64, int64) {
	var fresh, backlog int64
	_ = db.Raw(`SELECT count(*) FILTER (WHERE run_id=?),count(*) FILTER (WHERE run_id=?) FROM telemetry_inbox WHERE processed_at IS NULL AND ts_ms>=?`, freshRun, backlogRun, floor).Row().Scan(&fresh, &backlog)
	return fresh, backlog
}

func s04SamplePending(db *gorm.DB, freshRun, backlogRun string, floor int64, started time.Time, stop <-chan struct{}) []s04PendingSample {
	out := []s04PendingSample{}
	ticker := time.NewTicker(500 * time.Millisecond)
	defer ticker.Stop()
	for {
		select {
		case <-stop:
			return out
		case <-ticker.C:
			f, b := s04PendingPair(db, freshRun, backlogRun, floor)
			out = append(out, s04PendingSample{AtS: time.Since(started).Seconds(), Fresh: f, Backlog: b})
		}
	}
}

func s04Slope(points []s04PendingSample, pick func(s04PendingSample) float64) float64 {
	if len(points) < 2 {
		return 0
	}
	var sx, sy, sxx, sxy float64
	n := float64(len(points))
	for _, p := range points {
		x, y := p.AtS, pick(p)
		sx += x
		sy += y
		sxx += x * x
		sxy += x * y
	}
	den := n*sxx - sx*sx
	if math.Abs(den) < 1e-9 {
		return 0
	}
	return (n*sxy - sx*sy) / den
}

type s04RunCounts struct {
	Total        int64
	Success      int64
	SuccessAll   int64
	Pending      int64
	Retry        int64
	Quarantined  int64
	WithAttempts int64
	MaxAttempts  int64
}

func s04CountRun(t *testing.T, db *gorm.DB, runID string, start, end time.Time) s04RunCounts {
	t.Helper()
	var c s04RunCounts
	if err := db.Raw(`SELECT count(*),
		count(*) FILTER (WHERE processed_at IS NOT NULL AND processed_at>=? AND processed_at<=? AND business_state='success'),
		count(*) FILTER (WHERE business_state='success'),
		count(*) FILTER (WHERE processed_at IS NULL),
		count(*) FILTER (WHERE business_state='retry'),
		count(*) FILTER (WHERE business_state='quarantined'),
		count(*) FILTER (WHERE attempts>0),
		COALESCE(max(attempts),0)
		FROM telemetry_inbox WHERE run_id=?`, start, end, runID).
		Row().Scan(&c.Total, &c.Success, &c.SuccessAll, &c.Pending, &c.Retry, &c.Quarantined, &c.WithAttempts, &c.MaxAttempts); err != nil {
		t.Fatalf("count run %s: %v", runID, err)
	}
	return c
}

func s04SQLMillis(t *testing.T, db *gorm.DB, query string, args ...any) []float64 {
	t.Helper()
	rows, err := db.Raw(query, args...).Rows()
	if err != nil {
		t.Fatalf("latency query: %v", err)
	}
	defer rows.Close()
	out := []float64{}
	for rows.Next() {
		var v float64
		if err := rows.Scan(&v); err != nil {
			t.Fatalf("latency scan: %v", err)
		}
		out = append(out, v)
	}
	return out
}

func s04FailureCodes(t *testing.T, db *gorm.DB, runIDs ...string) map[string]int64 {
	t.Helper()
	out := map[string]int64{}
	for _, runID := range runIDs {
		rows, err := db.Raw(`SELECT COALESCE(failure_code,'none'),count(*) FROM telemetry_inbox WHERE run_id=? GROUP BY 1 ORDER BY 1`, runID).Rows()
		if err != nil {
			t.Fatalf("failure codes: %v", err)
		}
		for rows.Next() {
			var code string
			var n int64
			if err := rows.Scan(&code, &n); err != nil {
				rows.Close()
				t.Fatalf("failure code scan: %v", err)
			}
			out[code] += n
		}
		rows.Close()
	}
	return out
}

type s04FIFOReport struct {
	Pairs             int     `json:"pairs"`
	Inversions        int     `json:"processed_at_inversions"`
	BarrierViolations int     `json:"pending_barrier_violations"`
	MaxBackstepMS     float64 `json:"max_backstep_ms"`
}

func s04CheckFIFO(t *testing.T, db *gorm.DB, runID string) s04FIFOReport {
	t.Helper()
	type rec struct {
		DeviceID    int64
		ID          int64
		ProcessedAt *time.Time
	}
	var rows []rec
	if err := db.Raw(`SELECT device_id,id,processed_at FROM telemetry_inbox WHERE run_id=? ORDER BY device_id,id`, runID).Scan(&rows).Error; err != nil {
		t.Fatalf("fifo query: %v", err)
	}
	out := s04FIFOReport{}
	var prevDevice int64 = -1
	var prevProcessed *time.Time
	var firstPending int64
	for _, r := range rows {
		if r.DeviceID != prevDevice {
			prevDevice = r.DeviceID
			prevProcessed = nil
			firstPending = 0
		}
		if r.ProcessedAt == nil {
			if firstPending == 0 {
				firstPending = r.ID
			}
			continue
		}
		if firstPending != 0 && r.ID > firstPending {
			out.BarrierViolations++
		}
		if prevProcessed != nil {
			out.Pairs++
			if r.ProcessedAt.Before(prevProcessed.Add(-50 * time.Millisecond)) {
				out.Inversions++
				if ms := float64(prevProcessed.Sub(*r.ProcessedAt).Microseconds()) / 1000; ms > out.MaxBackstepMS {
					out.MaxBackstepMS = ms
				}
			}
		}
		at := *r.ProcessedAt
		prevProcessed = &at
	}
	return out
}

func s04Duplicates(t *testing.T, db *gorm.DB, runID string) int64 {
	t.Helper()
	var n int64
	if err := db.Raw(`SELECT count(*) FROM (SELECT device_id,seq FROM telemetry_inbox WHERE run_id=? GROUP BY 1,2 HAVING count(*)>1) x`, runID).Scan(&n).Error; err != nil {
		t.Fatalf("duplicate query: %v", err)
	}
	return n
}

func s04CountSimple(t *testing.T, db *gorm.DB, query string, args ...any) int64 {
	t.Helper()
	var n int64
	if err := db.Raw(query, args...).Scan(&n).Error; err != nil {
		t.Fatalf("count query: %v", err)
	}
	return n
}

func s04WriteJSON(t *testing.T, path string, v any) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Errorf("mkdir %s: %v", filepath.Dir(path), err)
		return
	}
	b, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		t.Errorf("marshal %s: %v", path, err)
		return
	}
	if err := os.WriteFile(path, b, 0o644); err != nil {
		t.Errorf("write %s: %v", path, err)
	}
}

func s04SafeName(s string) string {
	repl := []string{" ", "_", "/", "_", "\\", "_", ":", "_"}
	return strings.NewReplacer(repl...).Replace(s)
}

func s04SleepUntil(at time.Time) {
	if d := time.Until(at); d > 0 {
		time.Sleep(d)
	}
}

func s04WaitGroup(wg *sync.WaitGroup, d time.Duration) bool {
	done := make(chan struct{})
	go func() { wg.Wait(); close(done) }()
	select {
	case <-done:
		return true
	case <-time.After(d):
		return false
	}
}

type s04PathStat struct {
	Count  uint64  `json:"count"`
	MeanMS float64 `json:"mean_ms"`
	MaxMS  float64 `json:"max_ms"`
}

func s04PathStatsOf(in map[string]any) map[string]s04PathStat {
	out := map[string]s04PathStat{}
	for k, v := range in {
		m, ok := v.(map[string]any)
		if !ok {
			continue
		}
		st := s04PathStat{}
		if n, ok := m["count"].(uint64); ok {
			st.Count = n
		}
		if f, ok := m["mean_ms"].(float64); ok {
			st.MeanMS = f
		}
		if f, ok := m["max_ms"].(float64); ok {
			st.MaxMS = f
		}
		out[k] = st
	}
	return out
}

// ---------- 混合负载结果结构 ----------

type s04MixedInput struct {
	FleetSize        int     `json:"fleet_devices"`
	BacklogPerDevice int     `json:"backlog_per_device"`
	BacklogRows      int     `json:"backlog_rows"`
	FreshPlanned     int     `json:"fresh_planned"`
	ProducerSent     int     `json:"producer_sent"`
	RateTarget       float64 `json:"rate_target_per_second"`
	DurationSeconds  float64 `json:"duration_seconds"`
	Parallelism      int     `json:"history_parallelism"`
	S02              bool    `json:"s02_expiry_on"`
	S03              bool    `json:"s03_missing_circuit_on"`
	Counted          bool    `json:"sql_counter"`
	RealWorker       bool    `json:"real_worker_loop"`
	TDSeededRows     int     `json:"td_seeded_rows"`
	TDWriteDelayMS   float64 `json:"td_write_delay_target_ms"`
	TDWrittenRows    int     `json:"td_written_during_run"`
	TDWriteErrors    int     `json:"td_write_errors"`
	SeedSeconds      float64 `json:"seed_seconds"`
	LeadSeconds      float64 `json:"lead_seconds"`
	WarmupSeconds    float64 `json:"warmup_seconds"`
	StartDriftMS     float64 `json:"start_drift_ms"`
}

type s04MixedThroughput struct {
	Attempted            int     `json:"attempted"`
	Sent                 int     `json:"sent"`
	Accepted             int64   `json:"accepted"`
	LateInput            int64   `json:"late_input"`
	FreshSuccess         int64   `json:"fresh_success_in_window"`
	BacklogSuccess       int64   `json:"backlog_success_in_window"`
	TotalSuccess         int64   `json:"total_success_in_window"`
	SuccessPerSecond     float64 `json:"success_per_second"`
	InputPerSecond       float64 `json:"input_per_second"`
	NetHeadroomPerSecond float64 `json:"net_headroom_per_second"`
	CapacityValid        bool    `json:"capacity_valid"`
	CapacityNote         string  `json:"capacity_note"`
	DrainSuccess         int64   `json:"drain_success"`
	DrainSeconds         float64 `json:"drain_seconds"`
	DrainPerSecond       float64 `json:"drain_per_second"`
	Drained              bool    `json:"drained"`
	ProducerMaxLagMS     float64 `json:"producer_max_lag_ms"`
	SlowEnqueues         int     `json:"slow_enqueues"`
	MaxEnqueueMS         float64 `json:"max_enqueue_ms"`
}

type s04MixedPending struct {
	StartFresh            int64              `json:"start_fresh"`
	StartBacklog          int64              `json:"start_backlog"`
	WindowFresh           int64              `json:"window_end_fresh"`
	WindowBacklog         int64              `json:"window_end_backlog"`
	EndFresh              int64              `json:"end_fresh"`
	EndBacklog            int64              `json:"end_backlog"`
	PeakFresh             int64              `json:"peak_fresh"`
	PeakBacklog           int64              `json:"peak_backlog"`
	FreshSlopePerSecond   float64            `json:"fresh_slope_per_second"`
	BacklogSlopePerSecond float64            `json:"backlog_slope_per_second"`
	Curve                 []s04PendingSample `json:"curve"`
}

type s04MixedLatency struct {
	PerItem                      s04Latency   `json:"per_item"`
	Tick                         s04Latency   `json:"tick"`
	TD                           s04Latency   `json:"td"`
	ReceiveToSuccess             s04MSLatency `json:"receive_to_success"`
	ReceiveToConfirm             s04MSLatency `json:"receive_to_confirm"`
	EmitToAck                    s04MSLatency `json:"emit_to_ack"`
	EmitToEnqueue                s04MSLatency `json:"emit_to_enqueue"`
	TDWriteLag                   s04MSLatency `json:"td_write_visible_lag"`
	ReceiveToSuccessThroughDrain s04MSLatency `json:"receive_to_success_through_drain"`
}

type s04MixedWorkers struct {
	Ticks             int     `json:"ticks"`
	TicksWithWork     int     `json:"ticks_with_work"`
	HeadsMax          int     `json:"heads_per_tick_max"`
	HeadsMean         float64 `json:"heads_per_tick_mean"`
	Visits            int     `json:"scheduler_visits"`
	ItemsPerTickMean  float64 `json:"items_per_tick_mean"`
	EnergyDailyRows   int64   `json:"energy_daily_rows"`
	EnergyDailyNew    int64   `json:"energy_daily_updated_in_window"`
	EnergyDirtyRows   int64   `json:"energy_dirty_rows"`
	EnergyDirtyStart  int64   `json:"energy_dirty_at_start"`
	SnapshotRows      int64   `json:"telemetry_snapshot_rows"`
	RedisKeys         int64   `json:"redis_device_keys"`
	RedisWrites       uint64  `json:"redis_writes"`
	SnapshotUnchanged uint64  `json:"snapshot_unchanged"`
	TDQueryHit        uint64  `json:"td_query_hit"`
	TDQueryMissing    uint64  `json:"td_query_missing"`
	TDQueryError      uint64  `json:"td_query_error"`
	TDFailed          int64   `json:"td_proxy_failed"`
	SuccessCounter    uint64  `json:"success_counter"`
	RetryCounter      uint64  `json:"retry_counter"`
	QuarantineCounter uint64  `json:"quarantine_counter"`
	SchedulerVisit    uint64  `json:"scheduler_visit_counter"`
	SchedulerBudget   uint64  `json:"scheduler_budget_counter"`
	LoopCounter       uint64  `json:"loop_counter"`
}

type s04MixedResources struct {
	ElapsedSeconds       float64    `json:"elapsed_seconds"`
	ProcessCPUSeconds    float64    `json:"process_cpu_seconds"`
	ProcessCPUSecondsAll float64    `json:"process_cpu_seconds_total"`
	ProcessCoresAvg      float64    `json:"process_cores_avg"`
	PG                   s04PGDelta `json:"pg_database_delta"`
	PGTotal              s04PGDelta `json:"pg_database_delta_total"`
	PoolMaxInUse         int        `json:"pool_max_in_use"`
	PoolWaitCount        int64      `json:"pool_wait_count"`
	PoolWaitMS           float64    `json:"pool_wait_ms"`
	CgroupPGSec          float64    `json:"cgroup_pg_cpu_seconds"`
	CgroupTDSec          float64    `json:"cgroup_td_cpu_seconds"`
	CgroupRedisSec       float64    `json:"cgroup_redis_cpu_seconds"`
	CgroupPGSecTotal     float64    `json:"cgroup_pg_cpu_seconds_total"`
	CgroupTDSecTotal     float64    `json:"cgroup_td_cpu_seconds_total"`
	CgroupRedisSecTotal  float64    `json:"cgroup_redis_cpu_seconds_total"`
	CgroupAvailable      bool       `json:"cgroup_available"`
	GOMAXPROCS           int        `json:"gomaxprocs"`
}

type s04MixedCorrectness struct {
	Duplicates            int64            `json:"duplicates"`
	FIFOPairs             int              `json:"fifo_pairs"`
	FIFOInversions        int              `json:"fifo_processed_at_inversions"`
	FIFOBarrierViolations int              `json:"fifo_pending_barrier_violations"`
	FIFOMaxBackstepMS     float64          `json:"fifo_max_backstep_ms"`
	FailureCodes          map[string]int64 `json:"failure_codes"`
	RetryRows             int64            `json:"retry_rows"`
	QuarantinedRows       int64            `json:"quarantined_rows"`
	WithAttempts          int64            `json:"rows_with_attempts"`
	MaxAttempts           int64            `json:"max_attempts"`
	AckMissing            int              `json:"ack_missing"`
	AckCount              int              `json:"ack_count"`
	TDNotConfirmed        int64            `json:"success_without_td_confirm"`
	IdentityMismatch      int64            `json:"identity_mismatch"`
	InvariantsOK          bool             `json:"invariants_ok"`
	InvariantNotes        []string         `json:"invariant_notes"`
}

type s04MixedResult struct {
	Scenario     string                 `json:"scenario"`
	Label        string                 `json:"label"`
	Round        int                    `json:"round"`
	StartedAt    string                 `json:"started_at"`
	Evidence     string                 `json:"evidence_path"`
	Input        s04MixedInput          `json:"input"`
	Throughput   s04MixedThroughput     `json:"throughput"`
	Pending      s04MixedPending        `json:"pending"`
	Latency      s04MixedLatency        `json:"latency"`
	Workers      s04MixedWorkers        `json:"workers"`
	PathStats    map[string]s04PathStat `json:"telemetry_paths"`
	PGStatements map[string]any         `json:"pg_statements"`
	Resources    s04MixedResources      `json:"resources"`
	Correctness  s04MixedCorrectness    `json:"correctness"`
	Counters     map[string]uint64      `json:"telemetry_counters"`
	Logs         map[string]int64       `json:"log_events"`
	Notes        []string               `json:"notes"`
}

type s04MixedAggregate struct {
	Scenario    string             `json:"scenario"`
	Label       string             `json:"label"`
	Rounds      int                `json:"rounds"`
	Spec        map[string]any     `json:"spec"`
	Median      map[string]float64 `json:"median"`
	PerRound    []s04MixedResult   `json:"per_round"`
	GeneratedAt string             `json:"generated_at"`
}

func s04MedianF(v []float64) float64 {
	if len(v) == 0 {
		return 0
	}
	xs := append([]float64(nil), v...)
	sort.Float64s(xs)
	if len(xs)%2 == 1 {
		return xs[len(xs)/2]
	}
	return (xs[len(xs)/2-1] + xs[len(xs)/2]) / 2
}

// ---------- 单轮执行 ----------

func s04RunMixedRound(t *testing.T, env s04Env, tdURL string, tdStats *s04TDStats, spec s04MixedSpec, round int, outDir, label string) s04MixedResult {
	t.Helper()
	backlogRun := fmt.Sprintf("bl-r%02d", round)
	freshRun := fmt.Sprintf("fr-r%02d", round)
	res := s04MixedResult{
		Scenario: spec.Name, Label: label, Round: round,
		StartedAt: time.Now().UTC().Format(time.RFC3339),
	}

	logCounter := newS04LogCounter()
	zapLogger := zap.New(logCounter)
	counter := newS04QueryCounter()
	var gormLog logger.Interface = logger.Default.LogMode(logger.Silent)
	if spec.Counted {
		gormLog = counter
	}

	seedBegan := time.Now()
	db, pool := s04Open(t, env.dsn, gormLog)
	defer pool.Close()
	s04ResetSchema(t, db)
	devices := s04SeedDevices(t, db, s04MixedFleetSpecs(t, spec.FleetSize))

	rd := redis.NewClient(&redis.Options{Addr: env.redis, ContextTimeoutEnabled: true, MaxRetries: -1})
	defer rd.Close()
	if err := rd.Ping(context.Background()).Err(); err != nil {
		t.Fatalf("fixture Redis: %v", err)
	}
	if err := rd.FlushDB(context.Background()).Err(); err != nil {
		t.Fatalf("flush fixture Redis: %v", err)
	}

	cfg := config.Config{
		TDURL: tdURL, TDUser: "root", TDPassword: "taosdata", TDDatabase: s04FixtureTDDb,
		JWTSecret: strings.Repeat("x", 32), SimulationAlarmMode: "off",
		FleetPath:                  "../../../01-data-chain/simulator/fleet.json",
		InboxExpiryEnabled:         spec.S02,
		InboxExpiryBatch:           2000,
		InboxExpiryIntervalSeconds: 30,
		InboxExpiryBudgetSeconds:   5,
		TDMissingCircuitEnabled:    spec.S03,
	}
	td := tdengine.New(cfg)
	s04ResetTD(t, td)
	if spec.TDWriteDelay > 0 {
		// A missing row must be (false,nil), not an error from a missing child
		// table, otherwise the pre-existing error circuit opens immediately.
		s04EnsureFixtureTDTables(t, td, devices)
	}
	backlogBase := time.Now().UTC().Add(-2 * time.Hour)
	tdRows := s04SeedBacklogTD(t, td, devices, backlogRun, spec.BacklogPerDevice, backlogBase)
	backlogRows := 0
	if spec.BacklogPerDevice > 0 {
		s04SeedInbox(t, db, devices, backlogRun, spec.BacklogPerDevice, backlogBase, backlogBase)
		backlogRows = len(devices) * spec.BacklogPerDevice
	}

	start := time.Now().UTC().Add(12 * time.Second)
	plan, freshByDevice, freshPlanned, err := s04PlanFresh(devices, freshRun, spec.Rate, spec.Duration, start)
	if err != nil {
		t.Fatalf("plan fresh input: %v", err)
	}
	if spec.TDWriteDelay == 0 {
		tdRows += s04SeedFreshTD(t, td, devices, freshByDevice)
	}
	seedSeconds := time.Since(seedBegan).Seconds()
	if err := db.Exec(`UPDATE telemetry_scheduler SET last_device_id=0,realtime_last_device_id=0,history_enabled=true,history_parallelism=? WHERE id=1`, spec.Parallelism).Error; err != nil {
		t.Fatalf("set scheduler: %v", err)
	}

	srv := New(cfg, db, rd, td, zapLogger)
	ctx, cancel := context.WithCancel(context.Background())
	srv.runContext = ctx
	defer func() {
		cancel()
		if srv.realtime.cancel != nil {
			srv.realtime.cancel()
			<-srv.realtime.done
		}
	}()

	workerCtx, workerCancel := context.WithCancel(ctx)
	var workerWG sync.WaitGroup
	launch := func(fn func(context.Context)) {
		workerWG.Add(1)
		go func() { defer workerWG.Done(); fn(workerCtx) }()
	}
	// 预热只给 200ms，让依赖 worker 在窗口前完成调度；不存在独立预热负载。
	warmup := 200 * time.Millisecond
	workerStart := start.Add(-warmup)

	// 1) 依赖 CPU 基线（在 worker 启动前）。
	s04SleepUntil(start.Add(-1500 * time.Millisecond))
	cgroupBefore := map[string]float64{}
	cgroupAvailable := false
	if os.Getenv("IR_S04_CGROUP") == "1" {
		var mu sync.Mutex
		var wg sync.WaitGroup
		okAll := true
		for _, name := range []string{"ir-s04-pg", "ir-s04-td", "ir-s04-redis"} {
			wg.Add(1)
			go func(n string) {
				defer wg.Done()
				v, ok := s04CgroupCPU(n)
				mu.Lock()
				if ok {
					cgroupBefore[n] = v
				} else {
					okAll = false
				}
				mu.Unlock()
			}(name)
		}
		wg.Wait()
		cgroupAvailable = okAll
	}

	// 2) 处理基线：在 worker 启动前取，种子/建库成本不计入。
	s04SleepUntil(start.Add(-300 * time.Millisecond))
	pgBefore := s04ReadPG(t, db)
	cpuBefore := s04CPU()
	poolBefore := pool.Stats()
	tdStats.reset()
	counter.Reset()
	logCounter.reset()
	countersBefore := srv.telemetryCounters()
	pathsBefore := s04PathStatsOf(srv.telemetry.durations())
	floor := start.Add(-inboxWindow).UnixMilli()
	freshAtStart, backlogAtStart := s04PendingPair(db, freshRun, backlogRun, floor)
	energyDirtyStart := s04CountSimple(t, db, `SELECT count(*) FROM energy_dirty_days`)

	// 3) 启动生产同款依赖 worker，再把输入管线与历史循环拉到起点。
	s04SleepUntil(workerStart)
	launch(srv.snapshotWorker)
	launch(srv.energyWorker)
	if spec.S02 {
		launch(srv.expiryWorker)
	}
	srv.ensureRealtime()
	s04SleepUntil(start)
	measureStart := time.Now().UTC()

	ingressStop := s04StartIngress(ctx, srv)
	historyCtx, historyCancel := context.WithCancel(ctx)
	loopStop := make(chan struct{})
	loopDone := make(chan struct{})
	var loopStats s04LoopStats
	if spec.RealWorker {
		go func() { defer close(loopDone); srv.worker(historyCtx) }()
	} else {
		go func() { defer close(loopDone); s04MixedLoop(historyCtx, srv, loopStop, &loopStats) }()
	}

	producerStop := make(chan struct{})
	var producerStats s04ProducerStats
	var producerWG sync.WaitGroup
	producerWG.Add(1)
	go func() { defer producerWG.Done(); s04RunProducer(srv, plan, producerStop, &producerStats) }()
	var tdWriterStats s04TDWriterStats
	var tdWriterDone chan struct{}
	var tdWriterCancel context.CancelFunc
	if spec.TDWriteDelay > 0 {
		writerCfg := cfg
		writerCfg.TDURL = env.td
		writer := tdengine.New(writerCfg)
		writerCtx, stopWriter := context.WithCancel(ctx)
		tdWriterCancel = stopWriter
		tdWriterDone = make(chan struct{})
		go func() {
			defer close(tdWriterDone)
			s04RunDelayedTD(writerCtx, writer, plan, spec.TDWriteDelay, &tdWriterStats)
		}()
	}

	samplerStop := make(chan struct{})
	samplerDone := make(chan struct{})
	var pendingCurve []s04PendingSample
	go func() {
		defer close(samplerDone)
		pendingCurve = s04SamplePending(db, freshRun, backlogRun, floor, measureStart, samplerStop)
	}()
	poolPeakStop := make(chan struct{})
	var poolPeak atomic.Int64
	go func() {
		ticker := time.NewTicker(50 * time.Millisecond)
		defer ticker.Stop()
		for {
			select {
			case <-poolPeakStop:
				return
			case <-ticker.C:
				if v := int64(pool.Stats().InUse); v > poolPeak.Load() {
					poolPeak.Store(v)
				}
			}
		}
	}()

	measureEnd := measureStart.Add(spec.Duration)
	s04SleepUntil(measureEnd)
	close(producerStop)
	close(samplerStop)
	producerOK := s04WaitGroup(&producerWG, 30*time.Second)
	if !producerOK {
		// 输入背压已阻塞在准入队列；取消上下文解除阻塞，之后不再宣称有 drain。
		cancel()
	}
	<-samplerDone

	// 窗口末快照：容量组必须在这里判断积压是否仍在，不能用 drain 后归零的队列。
	freshAtWindowEnd, backlogAtWindowEnd := s04PendingPair(db, freshRun, backlogRun, floor)
	cpuMid := s04CPU()
	pgMid := s04ReadPG(t, db)
	poolMid := pool.Stats()
	countersMid := srv.telemetryCounters()
	pathsMid := s04PathStatsOf(srv.telemetry.durations())
	logsMid := logCounter.snapshot()
	tdWindow, tdFailedWindow := tdStats.reset()
	windowPerItem, windowTicks, windowHeads, windowVisits := loopStats.snapshot()
	var stmtSnapshotMid map[string]any
	if spec.Counted {
		stmtSnapshotMid = counter.Snapshot(freshPlanned)
	}
	cgroupMid := map[string]float64{}
	if cgroupAvailable {
		var mu sync.Mutex
		var wg sync.WaitGroup
		okAll := true
		for _, name := range []string{"ir-s04-pg", "ir-s04-td", "ir-s04-redis"} {
			wg.Add(1)
			go func(n string) {
				defer wg.Done()
				v, ok := s04CgroupCPU(n)
				mu.Lock()
				if ok {
					cgroupMid[n] = v
				} else {
					okAll = false
				}
				mu.Unlock()
			}(name)
		}
		wg.Wait()
		cgroupAvailable = okAll
	}
	if tdWriterDone != nil {
		select {
		case <-tdWriterDone:
		case <-time.After(15 * time.Second):
			tdWriterCancel()
			<-tdWriterDone
			t.Errorf("IR-S04 mixed round %d: delayed TD writer exceeded 15s post-window", round)
		}
	}
	tdWritten, tdWriteErrors, tdWriteLags := tdWriterStats.snapshot()

	drained := false
	if producerOK {
		for time.Since(measureEnd) < spec.Drain {
			f, _ := s04PendingPair(db, freshRun, backlogRun, floor)
			if f == 0 {
				drained = true
				break
			}
			time.Sleep(200 * time.Millisecond)
		}
	}
	if spec.RealWorker {
		// 真实 worker 没有 stop channel：先让当前 tick 结束，再取消。
		time.Sleep(2 * time.Second)
		historyCancel()
	} else {
		close(loopStop)
	}
	loopOK := false
	select {
	case <-loopDone:
		loopOK = true
	case <-time.After(3 * time.Minute):
		t.Errorf("IR-S04 mixed round %d: history loop did not stop within 3m", round)
	}
	drainEnd := time.Now().UTC()
	close(poolPeakStop)
	historyCancel()
	cancel()
	workerCancel()
	if !s04WaitGroup(&workerWG, 2*time.Minute) {
		t.Errorf("IR-S04 mixed round %d: dependency workers did not stop", round)
	}
	ingressStop()
	_, _ = tdStats.reset()

	// 4) 结束态与资源。窗口口径用 mid 快照；drain 只进 total。
	pgAfter := s04ReadPG(t, db)
	cpuAfter := s04CPU()
	cgroupAfter := map[string]float64{}
	if cgroupAvailable {
		for _, name := range []string{"ir-s04-pg", "ir-s04-td", "ir-s04-redis"} {
			if v, ok := s04CgroupCPU(name); ok {
				cgroupAfter[name] = v
			} else {
				cgroupAvailable = false
			}
		}
	}
	pathStats := s04PathDelta(pathsBefore, pathsMid)
	counters := s04CounterDelta(countersBefore, countersMid)
	logs := logsMid

	freshCounts := s04CountRun(t, db, freshRun, measureStart, measureEnd)
	backlogCounts := s04CountRun(t, db, backlogRun, measureStart, measureEnd)
	accepted := s04CountSimple(t, db, `SELECT count(*) FROM telemetry_inbox WHERE run_id=? AND received_at<=?`, freshRun, measureEnd)
	lateInput := s04CountSimple(t, db, `SELECT count(*) FROM telemetry_inbox WHERE run_id=? AND received_at>?`, freshRun, measureEnd)
	freshEnd, backlogEnd := s04PendingPair(db, freshRun, backlogRun, floor)
	drainSuccess := s04CountSimple(t, db, `SELECT count(*) FROM telemetry_inbox WHERE run_id IN (?,?) AND business_state='success' AND processed_at>? AND processed_at<=?`, freshRun, backlogRun, measureEnd, drainEnd)
	drainSeconds := drainEnd.Sub(measureEnd).Seconds()
	if drainSeconds < 0 {
		drainSeconds = 0
	}

	perItem, ticks, heads, visits := windowPerItem, windowTicks, windowHeads, windowVisits
	headsMax, headsSum, ticksWithWork := 0, 0, 0
	for _, h := range heads {
		if h > 0 {
			ticksWithWork++
		}
		if h > headsMax {
			headsMax = h
		}
		headsSum += h
	}
	headsMean := 0.0
	if len(heads) > 0 {
		headsMean = float64(headsSum) / float64(len(heads))
	}

	r2s := s04SQLMillis(t, db, `SELECT (EXTRACT(EPOCH FROM (processed_at-received_at))*1000)::double precision FROM telemetry_inbox WHERE run_id=? AND business_state='success' AND processed_at>=? AND processed_at<=?`, freshRun, measureStart, measureEnd)
	r2sThroughDrain := s04SQLMillis(t, db, `SELECT (EXTRACT(EPOCH FROM (processed_at-received_at))*1000)::double precision FROM telemetry_inbox WHERE run_id=? AND business_state='success' AND processed_at>=? AND processed_at<=?`, freshRun, measureStart, drainEnd)
	confirm := s04SQLMillis(t, db, `SELECT (EXTRACT(EPOCH FROM (td_confirmed_at-received_at))*1000)::double precision FROM telemetry_inbox WHERE run_id=? AND td_confirmed_at>=? AND td_confirmed_at<=?`, freshRun, measureStart, measureEnd)
	var enqueueMS, ackMS []float64
	acked := 0
	for _, p := range plan {
		enq := p.msg.enqueuedAt.Load()
		if enq == 0 {
			continue
		}
		enqueueMS = append(enqueueMS, float64(enq-p.msg.target.UnixNano())/1e6)
		if ack := p.msg.ackAt.Load(); ack > 0 {
			acked++
			ackMS = append(ackMS, float64(ack-enq)/1e6)
		}
	}
	sent, slowEnqueues, maxLagMS, maxEnqueueMS := producerStats.snapshot()
	ackMissing := sent - acked
	if ackMissing < 0 {
		ackMissing = 0
	}

	energyDailyRows := s04CountSimple(t, db, `SELECT count(*) FROM energy_daily`)
	energyDailyNew := s04CountSimple(t, db, `SELECT count(*) FROM energy_daily WHERE updated_at>=?`, measureStart)
	energyDirtyRows := s04CountSimple(t, db, `SELECT count(*) FROM energy_dirty_days`)
	snapshotRows := s04CountSimple(t, db, `SELECT count(*) FROM telemetry_snapshots`)
	redisKeys := rd.DBSize(context.Background()).Val()

	dup := s04Duplicates(t, db, freshRun) + s04Duplicates(t, db, backlogRun)
	fifoFresh := s04CheckFIFO(t, db, freshRun)
	fifoBacklog := s04CheckFIFO(t, db, backlogRun)
	fifo := s04FIFOReport{
		Pairs:             fifoFresh.Pairs + fifoBacklog.Pairs,
		Inversions:        fifoFresh.Inversions + fifoBacklog.Inversions,
		BarrierViolations: fifoFresh.BarrierViolations + fifoBacklog.BarrierViolations,
		MaxBackstepMS:     math.Max(fifoFresh.MaxBackstepMS, fifoBacklog.MaxBackstepMS),
	}
	failures := s04FailureCodes(t, db, freshRun, backlogRun)
	tdNotConfirmed := s04CountSimple(t, db, `SELECT count(*) FROM telemetry_inbox WHERE run_id=? AND business_state='success' AND td_confirmed_at IS NULL`, freshRun)

	totalSuccess := freshCounts.Success + backlogCounts.Success
	windowSeconds := spec.Duration.Seconds()
	successPerSecond := float64(totalSuccess) / windowSeconds
	inputPerSecond := float64(accepted) / windowSeconds
	netHeadroom := successPerSecond - inputPerSecond
	capacityValid := spec.Capacity && backlogAtWindowEnd > 0 && backlogCounts.Success > 0
	capacityNote := "steady input below capacity: success rate equals input and is not a capacity estimate"
	switch {
	case spec.Capacity && capacityValid:
		capacityNote = "backlog remained at window end: success rate is an under-load capacity estimate"
	case spec.Capacity:
		capacityNote = "backlog exhausted before window end: capacity is understated"
	}
	drainPerSecond := 0.0
	if drainSeconds > 0 {
		drainPerSecond = float64(drainSuccess) / drainSeconds
	}

	res.Input = s04MixedInput{
		FleetSize: spec.FleetSize, BacklogPerDevice: spec.BacklogPerDevice, BacklogRows: backlogRows,
		FreshPlanned: freshPlanned, ProducerSent: sent, RateTarget: spec.Rate,
		DurationSeconds: spec.Duration.Seconds(), Parallelism: spec.Parallelism,
		S02: spec.S02, S03: spec.S03, Counted: spec.Counted, RealWorker: spec.RealWorker,
		TDSeededRows: tdRows, SeedSeconds: seedSeconds, LeadSeconds: 12,
		TDWriteDelayMS: float64(spec.TDWriteDelay.Microseconds()) / 1000,
		TDWrittenRows:  tdWritten, TDWriteErrors: tdWriteErrors,
		WarmupSeconds: warmup.Seconds(),
		StartDriftMS:  float64(measureStart.Sub(start).Microseconds()) / 1000,
	}
	res.Throughput = s04MixedThroughput{
		Attempted: freshPlanned, Sent: sent, Accepted: accepted, LateInput: lateInput,
		FreshSuccess: freshCounts.Success, BacklogSuccess: backlogCounts.Success, TotalSuccess: totalSuccess,
		SuccessPerSecond: successPerSecond, InputPerSecond: inputPerSecond, NetHeadroomPerSecond: netHeadroom,
		CapacityValid: capacityValid, CapacityNote: capacityNote,
		DrainSuccess: drainSuccess, DrainSeconds: drainSeconds, DrainPerSecond: drainPerSecond,
		Drained: drained || freshEnd == 0, ProducerMaxLagMS: maxLagMS,
		SlowEnqueues: slowEnqueues, MaxEnqueueMS: maxEnqueueMS,
	}
	res.Pending = s04MixedPending{
		StartFresh: freshAtStart, StartBacklog: backlogAtStart,
		WindowFresh: freshAtWindowEnd, WindowBacklog: backlogAtWindowEnd,
		EndFresh: freshEnd, EndBacklog: backlogEnd,
		PeakFresh: s04Peak(pendingCurve, true), PeakBacklog: s04Peak(pendingCurve, false),
		FreshSlopePerSecond:   s04Slope(pendingCurve, func(p s04PendingSample) float64 { return float64(p.Fresh) }),
		BacklogSlopePerSecond: s04Slope(pendingCurve, func(p s04PendingSample) float64 { return float64(p.Backlog) }),
		Curve:                 pendingCurve,
	}
	res.Latency = s04MixedLatency{
		PerItem: s04LatencyOf(perItem), Tick: s04LatencyOf(ticks), TD: s04LatencyOf(tdWindow),
		ReceiveToSuccess: s04LatencyOfMS(r2s), ReceiveToConfirm: s04LatencyOfMS(confirm),
		EmitToAck: s04LatencyOfMS(ackMS), EmitToEnqueue: s04LatencyOfMS(enqueueMS),
		TDWriteLag: s04LatencyOfMS(tdWriteLags), ReceiveToSuccessThroughDrain: s04LatencyOfMS(r2sThroughDrain),
	}
	res.Workers = s04MixedWorkers{
		Ticks: len(ticks), TicksWithWork: ticksWithWork, HeadsMax: headsMax, HeadsMean: headsMean,
		Visits: visits, ItemsPerTickMean: s04RatioFloat(float64(totalSuccess), float64(len(ticks))),
		EnergyDailyRows: energyDailyRows, EnergyDailyNew: energyDailyNew,
		EnergyDirtyRows: energyDirtyRows, EnergyDirtyStart: energyDirtyStart,
		SnapshotRows: snapshotRows, RedisKeys: redisKeys,
		RedisWrites: counters["redis_write"], SnapshotUnchanged: counters["snapshot_unchanged"],
		TDQueryHit: counters["td_query_hit"], TDQueryMissing: counters["td_query_missing"],
		TDQueryError: counters["td_query_error"], TDFailed: tdFailedWindow, SuccessCounter: counters["success"],
		RetryCounter: counters["retry"], QuarantineCounter: counters["quarantine"],
		SchedulerVisit: counters["scheduler_visit"], SchedulerBudget: counters["scheduler_budget"],
		LoopCounter: counters["loop"],
	}
	if spec.RealWorker {
		res.Workers.Visits = int(counters["scheduler_visit"])
		if st, ok := pathStats["loop"]; ok && st.Count > 0 {
			res.Workers.Ticks = int(st.Count)
			res.Workers.ItemsPerTickMean = float64(totalSuccess) / float64(st.Count)
		}
	}
	res.PathStats = pathStats
	res.Counters = counters
	res.Logs = logs
	res.PGStatements = stmtSnapshotMid
	if res.PGStatements == nil {
		res.PGStatements = map[string]any{"disabled": true}
	}
	res.Resources = s04MixedResources{
		ElapsedSeconds:    windowSeconds,
		ProcessCPUSeconds: cpuMid - cpuBefore, ProcessCPUSecondsAll: cpuAfter - cpuBefore,
		ProcessCoresAvg: (cpuMid - cpuBefore) / windowSeconds,
		PG:              pgMid.minus(pgBefore), PGTotal: pgAfter.minus(pgBefore),
		PoolMaxInUse:    int(poolPeak.Load()),
		PoolWaitCount:   poolMid.WaitCount - poolBefore.WaitCount,
		PoolWaitMS:      float64(poolMid.WaitDuration-poolBefore.WaitDuration) / 1e6,
		CgroupAvailable: cgroupAvailable, GOMAXPROCS: runtime.GOMAXPROCS(0),
	}
	if cgroupAvailable {
		res.Resources.CgroupPGSec = cgroupMid["ir-s04-pg"] - cgroupBefore["ir-s04-pg"]
		res.Resources.CgroupTDSec = cgroupMid["ir-s04-td"] - cgroupBefore["ir-s04-td"]
		res.Resources.CgroupRedisSec = cgroupMid["ir-s04-redis"] - cgroupBefore["ir-s04-redis"]
		res.Resources.CgroupPGSecTotal = cgroupAfter["ir-s04-pg"] - cgroupBefore["ir-s04-pg"]
		res.Resources.CgroupTDSecTotal = cgroupAfter["ir-s04-td"] - cgroupBefore["ir-s04-td"]
		res.Resources.CgroupRedisSecTotal = cgroupAfter["ir-s04-redis"] - cgroupBefore["ir-s04-redis"]
	}
	res.Correctness = s04MixedCorrectness{
		Duplicates: dup, FIFOPairs: fifo.Pairs, FIFOInversions: fifo.Inversions,
		FIFOBarrierViolations: fifo.BarrierViolations, FIFOMaxBackstepMS: fifo.MaxBackstepMS,
		FailureCodes: failures, RetryRows: freshCounts.Retry + backlogCounts.Retry,
		QuarantinedRows: freshCounts.Quarantined + backlogCounts.Quarantined,
		WithAttempts:    freshCounts.WithAttempts + backlogCounts.WithAttempts,
		AckMissing:      ackMissing, AckCount: acked, TDNotConfirmed: tdNotConfirmed,
		IdentityMismatch: failures["identity_invalid"],
	}
	res.Correctness.MaxAttempts = int64(math.Max(float64(freshCounts.MaxAttempts), float64(backlogCounts.MaxAttempts)))

	inv := []string{}
	if dup > 0 {
		inv = append(inv, fmt.Sprintf("duplicate (run_id,device_id,seq)=%d", dup))
	}
	if fifo.BarrierViolations > 0 {
		inv = append(inv, fmt.Sprintf("same-device pending barrier violations=%d", fifo.BarrierViolations))
	}
	if accepted == 0 {
		inv = append(inv, "no fresh input was durably accepted")
	}
	if tdNotConfirmed > 0 {
		inv = append(inv, fmt.Sprintf("success without td_confirmed_at=%d", tdNotConfirmed))
	}
	if res.Correctness.IdentityMismatch > 0 {
		inv = append(inv, fmt.Sprintf("identity_invalid=%d", res.Correctness.IdentityMismatch))
	}
	if !producerOK {
		inv = append(inv, "producer blocked on admission queue; drain not measured")
	}
	if !loopOK {
		inv = append(inv, "history loop did not stop")
	}
	if spec.TDWriteDelay > 0 && (tdWriteErrors != 0 || tdWritten != freshPlanned) {
		inv = append(inv, fmt.Sprintf("delayed TD writer written=%d planned=%d errors=%d", tdWritten, freshPlanned, tdWriteErrors))
	}
	res.Correctness.InvariantNotes = inv
	res.Correctness.InvariantsOK = len(inv) == 0
	if !res.Correctness.InvariantsOK {
		t.Errorf("IR-S04 mixed %s round %d invariants: %v", spec.Name, round, inv)
	}

	res.Notes = []string{
		"输入经真实 acceptTelemetry -> enqueueAdmission(QoS1 顺序 ACK) -> 4×receive -> inbox INSERT；未启动真实 MQTT broker/独立 gateway 进程；TD 行是否预写见 input.td_write_delay_target_ms。",
		"snapshotWorker(TD 实时确认 + telemetry_snapshots + Redis CAS) 与 energyWorker(日聚合重建) 已参与；S02/S03 开关见 input。",
		"历史循环是 processPendingBudget 的逐行镜像（同 SQL、同队头、同游标、同 8 路、同 500ms ticker），只在 processInboxItem 调用点加了计时。",
		"P99 用 nearest-rank，样本数 n 见各字段；稳态组 success 受输入限制，capacity 组才用于净余量换算。",
		"TD 延迟经 loopback 反代，含一次转发；energyWorker 的当日样本量远小于生产整日，其成本是下界；home-dev 与其它栈共机，资源读数有共租噪声。",
	}
	if spec.RealWorker {
		res.Notes = append(res.Notes, "realworker 模式使用真实 srv.worker，不采 per-item/heads 明细；以 telemetry history/loop 路径的 mean/max 替代。")
	}
	if !cgroupAvailable {
		res.Notes = append(res.Notes, "依赖 cgroup CPU 不可用（IR_S04_CGROUP!=1 或 docker exec 失败），仅报告 PG 内部计数与进程 CPU。")
	}
	if spec.TDWriteDelay > 0 {
		res.Notes = append(res.Notes, "本场景 TD 行在 API 排定接收后延迟写入一次性 TD 库；TDWriteLag 是从排定接收到 INSERT 返回的上界代理。真实 MQTT/gateway/SQLite 未参与，不能外推生产写入时延。")
	}
	res.Evidence = filepath.Join(outDir, fmt.Sprintf("%s-%s-round%d.json", spec.Name, s04SafeName(label), round))
	s04WriteJSON(t, res.Evidence, res)

	t.Logf("MIXED %s round=%d accepted=%d sent=%d success=%d (fresh=%d backlog=%d) rate=%.2f/s input=%.2f/s net=%.2f/s pending(f/b)=%d/%d | perItem p50=%.1f p95=%.1f p99=%.1f n=%d | tick p50=%.1f p99=%.1f n=%d | td p50=%.1f p99=%.1f n=%d | r2s p50=%.1f p95=%.1f p99=%.1f n=%d | cpu=%.2fs cores=%.2f",
		spec.Name, round, accepted, sent, totalSuccess, freshCounts.Success, backlogCounts.Success,
		successPerSecond, inputPerSecond, netHeadroom, freshEnd, backlogEnd,
		res.Latency.PerItem.P50MS, res.Latency.PerItem.P95MS, res.Latency.PerItem.P99MS, res.Latency.PerItem.N,
		res.Latency.Tick.P50MS, res.Latency.Tick.P99MS, res.Latency.Tick.N,
		res.Latency.TD.P50MS, res.Latency.TD.P99MS, res.Latency.TD.N,
		res.Latency.ReceiveToSuccess.P50MS, res.Latency.ReceiveToSuccess.P95MS, res.Latency.ReceiveToSuccess.P99MS, res.Latency.ReceiveToSuccess.N,
		res.Resources.ProcessCPUSeconds, res.Resources.ProcessCoresAvg)
	return res
}

func s04RatioFloat(n, d float64) float64 {
	if d <= 0 {
		return 0
	}
	return n / d
}

func s04Peak(curve []s04PendingSample, fresh bool) int64 {
	var peak int64
	for _, p := range curve {
		v := p.Fresh
		if !fresh {
			v = p.Backlog
		}
		if v > peak {
			peak = v
		}
	}
	return peak
}

func s04PathDelta(before, after map[string]s04PathStat) map[string]s04PathStat {
	out := map[string]s04PathStat{}
	for k, a := range after {
		b := before[k]
		cnt := a.Count - b.Count
		if cnt == 0 {
			continue
		}
		sumA := a.MeanMS * float64(a.Count)
		sumB := b.MeanMS * float64(b.Count)
		mean := (sumA - sumB) / float64(cnt)
		max := math.Max(a.MaxMS, b.MaxMS)
		out[k] = s04PathStat{Count: cnt, MeanMS: mean, MaxMS: max}
	}
	return out
}

func s04CounterDelta(before, after map[string]uint64) map[string]uint64 {
	out := map[string]uint64{}
	for k, v := range after {
		if d := v - before[k]; d > 0 {
			out[k] = d
		}
	}
	return out
}

// TestIRS04MixedCapacity 是第二轮混合负载入口。每组默认 3 轮；同一夹具与同
// 一输入在改动前后各跑一次，即可做「同输入改前/改后」对照：
//
//	IR_S04_SCENARIO=mixed-steady-143-24 IR_S04_ROUNDS=3 IR_S04_LABEL=before ...
//	IR_S04_SCENARIO=mixed-steady-143-24 IR_S04_ROUNDS=3 IR_S04_LABEL=after  ...
func TestIRS04MixedCapacity(t *testing.T) {
	env := s04RequireFixture(t)
	scenario := s04EnvOf("IR_S04_SCENARIO", "mixed-steady-143-24")
	spec, err := s04MixedSpecOf(scenario)
	if err != nil {
		t.Fatalf("IR-S04 mixed scenario: %v", err)
	}
	rounds := s04EnvInt("IR_S04_ROUNDS", 3)
	if rounds < 3 {
		t.Logf("IR-S04 mixed: rounds=%d < 3; this run is not sufficient for a repeatable baseline", rounds)
	}
	label := s04EnvOf("IR_S04_LABEL", "current")
	outDir := s04EnvOf("IR_S04_OUT", filepath.Join("..", "..", "..", "..", "artifacts", "ir-s04", s04MixedDefaultOut))
	if err := os.MkdirAll(outDir, 0o755); err != nil {
		t.Fatalf("create evidence dir %s: %v", outDir, err)
	}
	proxy, tdStats := s04StartTDProxy(t, env.td)
	t.Logf("IR-S04 mixed fixture: scenario=%s fleet=%d backlog/device=%d rate=%.2f/s duration=%s rounds=%d label=%s pg=%s redis=%s td=%s",
		spec.Name, spec.FleetSize, spec.BacklogPerDevice, spec.Rate, spec.Duration, rounds, label,
		s04MaskDSN(env.dsn), env.redis, env.td)
	var results []s04MixedResult
	for round := 1; round <= rounds; round++ {
		results = append(results, s04RunMixedRound(t, env, proxy.URL, tdStats, spec, round, outDir, label))
	}
	agg := s04AggregateMixed(spec, label, results)
	path := filepath.Join(outDir, fmt.Sprintf("%s-%s-summary.json", spec.Name, s04SafeName(label)))
	s04WriteJSON(t, path, agg)
	t.Logf("MIXED AGG %s label=%s rounds=%d success=%.2f/s input=%.2f/s net=%.2f/s perItemP99=%.1fms tickP99=%.1fms r2sP99=%.1fms cores=%.2f path=%s",
		spec.Name, label, len(results),
		agg.Median["success_per_second"], agg.Median["input_per_second"], agg.Median["net_headroom_per_second"],
		agg.Median["per_item_p99_ms"], agg.Median["tick_p99_ms"], agg.Median["receive_to_success_p99_ms"],
		agg.Median["process_cores_avg"], path)
}

func s04AggregateMixed(spec s04MixedSpec, label string, rounds []s04MixedResult) s04MixedAggregate {
	agg := s04MixedAggregate{
		Scenario: spec.Name, Label: label, Rounds: len(rounds), PerRound: rounds,
		GeneratedAt: time.Now().UTC().Format(time.RFC3339),
		Spec: map[string]any{
			"fleet_devices": spec.FleetSize, "backlog_per_device": spec.BacklogPerDevice,
			"rate": spec.Rate, "duration_seconds": spec.Duration.Seconds(),
			"history_parallelism": spec.Parallelism, "s02_expiry_on": spec.S02,
			"s03_missing_circuit_on": spec.S03, "capacity_group": spec.Capacity,
			"real_worker_loop": spec.RealWorker, "sql_counter": spec.Counted,
		},
		Median: map[string]float64{},
	}
	entry := func(name string, f func(s04MixedResult) float64) {
		vals := make([]float64, 0, len(rounds))
		for _, r := range rounds {
			vals = append(vals, f(r))
		}
		agg.Median[name] = s04MedianF(vals)
	}
	entry("success_per_second", func(r s04MixedResult) float64 { return r.Throughput.SuccessPerSecond })
	entry("input_per_second", func(r s04MixedResult) float64 { return r.Throughput.InputPerSecond })
	entry("net_headroom_per_second", func(r s04MixedResult) float64 { return r.Throughput.NetHeadroomPerSecond })
	entry("drain_per_second", func(r s04MixedResult) float64 { return r.Throughput.DrainPerSecond })
	entry("per_item_p50_ms", func(r s04MixedResult) float64 { return r.Latency.PerItem.P50MS })
	entry("per_item_p95_ms", func(r s04MixedResult) float64 { return r.Latency.PerItem.P95MS })
	entry("per_item_p99_ms", func(r s04MixedResult) float64 { return r.Latency.PerItem.P99MS })
	entry("tick_p50_ms", func(r s04MixedResult) float64 { return r.Latency.Tick.P50MS })
	entry("tick_p99_ms", func(r s04MixedResult) float64 { return r.Latency.Tick.P99MS })
	entry("td_p50_ms", func(r s04MixedResult) float64 { return r.Latency.TD.P50MS })
	entry("td_p99_ms", func(r s04MixedResult) float64 { return r.Latency.TD.P99MS })
	entry("receive_to_success_p50_ms", func(r s04MixedResult) float64 { return r.Latency.ReceiveToSuccess.P50MS })
	entry("receive_to_success_p95_ms", func(r s04MixedResult) float64 { return r.Latency.ReceiveToSuccess.P95MS })
	entry("receive_to_success_p99_ms", func(r s04MixedResult) float64 { return r.Latency.ReceiveToSuccess.P99MS })
	entry("receive_to_confirm_p99_ms", func(r s04MixedResult) float64 { return r.Latency.ReceiveToConfirm.P99MS })
	entry("emit_to_ack_p99_ms", func(r s04MixedResult) float64 { return r.Latency.EmitToAck.P99MS })
	entry("process_cores_avg", func(r s04MixedResult) float64 { return r.Resources.ProcessCoresAvg })
	entry("pg_xact_commit", func(r s04MixedResult) float64 { return float64(r.Resources.PG.XactCommit) })
	entry("pg_tup_inserted", func(r s04MixedResult) float64 { return float64(r.Resources.PG.TupInserted) })
	entry("pg_tup_updated", func(r s04MixedResult) float64 { return float64(r.Resources.PG.TupUpdated) })
	entry("pool_wait_count", func(r s04MixedResult) float64 { return float64(r.Resources.PoolWaitCount) })
	entry("pending_fresh_slope_per_second", func(r s04MixedResult) float64 { return r.Pending.FreshSlopePerSecond })
	entry("pending_backlog_slope_per_second", func(r s04MixedResult) float64 { return r.Pending.BacklogSlopePerSecond })
	return agg
}

func (s *Server) telemetryCounters() map[string]uint64 {
	s.telemetry.mu.Lock()
	defer s.telemetry.mu.Unlock()
	out := make(map[string]uint64, len(s.telemetry.counts))
	for k, v := range s.telemetry.counts {
		out[k] = v
	}
	return out
}
