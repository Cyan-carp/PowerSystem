//go:build ir_s03

package api

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"go.uber.org/zap"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"powersystem/backend/internal/config"
	"powersystem/backend/internal/model"
	"powersystem/backend/internal/store"
	"powersystem/backend/internal/tdengine"
)

// 只跑在一次性隔离库 ir_s03_test。没有专用 DSN 时必须失败而不是跳过，避免把
// “没连库”误报成通过；不会触碰 home-dev 业务库或生产。
var irS03DeviceRE = regexp.MustCompile(`t_device_(\d+)`)

type irS03Fixture struct {
	t        *testing.T
	db       *gorm.DB
	s        *Server
	tdServer *httptest.Server

	mode      atomic.Int32
	perDevice sync.Map
	calls     atomic.Int64
	// mode 3 的查询在阻塞前通知测试，确保取消发生在 TD 查询期间，而不是
	// 更早的数据库往返。
	queryStarted chan struct{}
}

func irS03FixtureNew(t *testing.T) *irS03Fixture {
	t.Helper()
	dsn := os.Getenv("IR_S03_TEST_DSN")
	if os.Getenv("IR_S03_ISOLATED") != "1" || dsn == "" {
		t.Fatal("isolated IR-S03 PostgreSQL required: set IR_S03_ISOLATED=1 and IR_S03_TEST_DSN")
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
	if env.DB != "ir_s03_test" || env.Port != 5432 || ip == nil || !(ip.IsLoopback() || ip.IsPrivate()) {
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

	f := &irS03Fixture{t: t, db: db, queryStarted: make(chan struct{}, 1)}
	f.tdServer = httptest.NewServer(http.HandlerFunc(f.handleTD))
	t.Cleanup(f.tdServer.Close)
	cfg := config.Config{
		TDURL:                   f.tdServer.URL,
		TDDatabase:              "ir_s03",
		SimulationAlarmMode:     "off",
		JWTSecret:               strings.Repeat("x", 32),
		TDMissingCircuitEnabled: true,
	}
	f.s = New(cfg, db, nil, tdengine.New(cfg), zap.NewNop())
	// 生产冻结值由单测锁定；隔离测试只缩短成熟/持续/冷却窗口，让状态转换在
	// 秒级完成。设备门槛保留生产默认 16 台，端到端验证跨设备开路确实生效。
	f.s.tdMissingCircuit.settings = tdMissingCircuitSettings{
		MatureAge: 100 * time.Millisecond,
		// 隔离库跨 SSH 往返，单轮 16 台设备可能耗时 >1s；窗口必须覆盖一轮，
		// 生产冻结值是 3 分钟，这里按测试往返放大到 30 秒。
		EvidenceWindow:  30 * time.Second,
		MinDeviceChecks: 2,
		ConfirmSpan:     10 * time.Millisecond,
		MinDevices:      tdMissingMinDevices,
		// 隔离库每次事务跨 SSH 往返可能 >50ms；若用毫秒级冷却，断言时已自动
		// 进入半开。冷却窗口由单测用假时钟精确验证，这里用 10 秒保持 open，
		// 需要半开时显式把 until 拨到过去。
		Cooldown:      10 * time.Second,
		EvidenceLimit: 64,
	}
	return f
}

func (f *irS03Fixture) handleTD(w http.ResponseWriter, r *http.Request) {
	f.calls.Add(1)
	body, _ := io.ReadAll(r.Body)
	deviceID := int64(0)
	if m := irS03DeviceRE.FindStringSubmatch(string(body)); len(m) == 2 {
		deviceID, _ = strconv.ParseInt(m[1], 10, 64)
	}
	mode := f.mode.Load()
	if raw, ok := f.perDevice.Load(deviceID); ok {
		mode = raw.(int32)
	}
	switch mode {
	case 1:
		_ = json.NewEncoder(w).Encode(map[string]any{"code": 0, "rows": 0, "data": nil})
		return
	case 2:
		http.Error(w, "fixture td error", http.StatusServiceUnavailable)
		return
	case 3:
		select {
		case f.queryStarted <- struct{}{}:
		default:
		}
		<-r.Context().Done()
		return
	}
	_ = json.NewEncoder(w).Encode(map[string]any{"code": 0, "rows": 1, "data": [][]any{{1}}})
}

func (f *irS03Fixture) setMode(deviceID int64, mode int32) {
	f.perDevice.Store(deviceID, mode)
}

func (f *irS03Fixture) device(code string) model.Device {
	f.t.Helper()
	d := model.Device{DeviceCode: code, Name: code, DevType: "inverter", StationCode: "IR-S03", Vendor: "fixture"}
	if err := f.db.Create(&d).Error; err != nil {
		f.t.Fatal(err)
	}
	return d
}

func (f *irS03Fixture) item(d model.Device, seq int64, received time.Time, ts int64) model.Inbox {
	f.t.Helper()
	payload := fmt.Sprintf(`{"schema_version":1,"run_id":"ir-s03","device_id":%q,"station_id":%q,"seq":%d,"ts_ms":%d,"voltage":400,"current":100,"power":68.5,"temperature":40,"status":1,"fault_code":0}`, d.DeviceCode, d.StationCode, seq, ts)
	var id int64
	if err := f.db.Raw(`INSERT INTO telemetry_inbox(run_id,device_id,seq,ts_ms,payload,received_at,business_state)
VALUES('ir-s03',?,?,?,?::jsonb,?,'pending') RETURNING id`, d.ID, seq, ts, payload, received).Scan(&id).Error; err != nil {
		f.t.Fatal(err)
	}
	return f.row(id)
}

func (f *irS03Fixture) row(id int64) model.Inbox {
	f.t.Helper()
	var row model.Inbox
	if err := f.db.Raw("SELECT * FROM telemetry_inbox WHERE id=?", id).Scan(&row).Error; err != nil {
		f.t.Fatal(err)
	}
	return row
}

func (f *irS03Fixture) retry(id int64) {
	f.t.Helper()
	if err := f.db.Exec(`UPDATE telemetry_inbox SET next_attempt_at=? WHERE id=?`, time.Now().Add(-time.Second), id).Error; err != nil {
		f.t.Fatal(err)
	}
}

func (f *irS03Fixture) metric(key string) uint64 {
	f.s.telemetry.mu.Lock()
	defer f.s.telemetry.mu.Unlock()
	return f.s.telemetry.counts[key]
}

func (f *irS03Fixture) state() float64 {
	return f.s.tdMissingCircuit.snapshot(time.Now())
}

func (f *irS03Fixture) expireCooldown() {
	f.s.tdMissingCircuit.mu.Lock()
	f.s.tdMissingCircuit.until = time.Now().Add(-time.Second)
	f.s.tdMissingCircuit.mu.Unlock()
}

func (f *irS03Fixture) success(row model.Inbox) bool {
	return row.ProcessedAt != nil && row.BusinessState != nil && *row.BusinessState == "success"
}

func (f *irS03Fixture) process(row model.Inbox) {
	f.t.Helper()
	f.s.processInboxItem(context.Background(), row)
}

// processCancelDuringQuery 在 TD 查询真正开始后取消父 context，确保测试覆盖
// “查询返回时已取消”的边界，而不是数据库阶段先被取消。
func (f *irS03Fixture) processCancelDuringQuery(row model.Inbox) {
	f.t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		defer close(done)
		f.s.processInboxItem(ctx, row)
	}()
	select {
	case <-f.queryStarted:
		cancel()
	case <-done:
		cancel()
		f.t.Fatal("process finished before TD query started")
	case <-time.After(10 * time.Second):
		cancel()
		<-done
		f.t.Fatal("TD query did not start before cancellation")
	}
	<-done
}

// 近期 false 只是 TD 尚未写完：即使多台设备、多次查无，也不得进入成熟证据；
// 随后真实命中应正常成功，不能留下半开/开路状态。
func TestIRS03RecentFalseThenHitIsolated(t *testing.T) {
	f := irS03FixtureNew(t)
	f.s.tdMissingCircuit.settings.MatureAge = time.Minute
	f.mode.Store(1)
	devices := make([]model.Device, 3)
	rows := make([]model.Inbox, 3)
	for i := range devices {
		devices[i] = f.device(fmt.Sprintf("IR-S03-R-%03d", i))
		rows[i] = f.item(devices[i], 1, time.Now(), time.Now().Add(-time.Second).UnixMilli())
	}
	for _, row := range rows {
		f.process(row)
	}
	if got := f.state(); got != 0 {
		t.Fatalf("recent false opened circuit: state=%v", got)
	}
	if got := f.metric("td_missing_mature"); got != 0 {
		t.Fatalf("recent false counted as mature: %d", got)
	}
	if got := f.metric("td_query_missing"); got != 3 {
		t.Fatalf("missing counter=%d want 3", got)
	}
	f.mode.Store(0)
	for i := range rows {
		f.retry(rows[i].ID)
		f.process(rows[i])
		row := f.row(rows[i].ID)
		if !f.success(row) {
			t.Fatalf("device %d did not succeed after TD hit: %+v", devices[i].ID, row)
		}
	}
	if got := f.metric("td_query_hit"); got != 3 {
		t.Fatalf("hit counter=%d want 3", got)
	}
	if got := f.state(); got != 0 {
		t.Fatalf("state after hits=%v", got)
	}
}

// 单设备永久空洞：同一条持续成熟查无只能算一台设备，绝不能开全局；其他设备
// 仍应正常命中并成功。
func TestIRS03SingleDeviceHoleIsolated(t *testing.T) {
	f := irS03FixtureNew(t)
	f.mode.Store(1)
	d := f.device("IR-S03-HOLE")
	row := f.item(d, 1, time.Now().Add(-2*time.Second), time.Now().Add(-2*time.Second).UnixMilli())
	for i := 0; i < 3; i++ {
		f.process(row)
		row = f.row(row.ID)
		time.Sleep(15 * time.Millisecond)
		f.retry(row.ID)
	}
	if got := f.state(); got != 0 {
		t.Fatalf("single device hole opened global circuit: state=%v", got)
	}
	if got := f.metric("td_missing_mature"); got < 3 {
		t.Fatalf("mature counter=%d want >=3", got)
	}
	if got := f.metric("td_missing_circuit_open"); got != 0 {
		t.Fatalf("open counter=%d want 0", got)
	}
	other := f.device("IR-S03-HEALTHY")
	f.setMode(other.ID, 0)
	otherRow := f.item(other, 1, time.Now(), time.Now().Add(-time.Second).UnixMilli())
	f.process(otherRow)
	if !f.success(f.row(otherRow.ID)) {
		t.Fatal("healthy device did not proceed while another device had a permanent hole")
	}
}

// 达到生产设备门槛、每台两次成熟且跨度足够的查无才开路；开路后其他设备只被
// 限流、不写失败证据；冷却后半开命中恢复，未完成设备继续成功。
func TestIRS03CrossDeviceOpenAndRecoverIsolated(t *testing.T) {
	f := irS03FixtureNew(t)
	f.mode.Store(1)
	n := int(tdMissingMinDevices)
	devices := make([]model.Device, n)
	rows := make([]model.Inbox, n)
	for i := range devices {
		devices[i] = f.device(fmt.Sprintf("IR-S03-OPEN-%03d", i))
		rows[i] = f.item(devices[i], 1, time.Now().Add(-2*time.Second), time.Now().Add(-2*time.Second).UnixMilli())
	}
	for _, row := range rows {
		f.process(row)
	}
	time.Sleep(15 * time.Millisecond)
	for _, row := range rows {
		f.retry(row.ID)
	}
	for i := 0; i < n-1; i++ {
		f.process(rows[i])
	}
	if got := f.state(); got != 0 {
		t.Fatalf("opened before device threshold: state=%v", got)
	}
	f.process(rows[n-1])
	if got := f.state(); got != 1 {
		t.Fatalf("cross-device mature missing did not open: state=%v", got)
	}
	if got := f.metric("td_missing_circuit_open"); got != 1 {
		t.Fatalf("open counter=%d want 1", got)
	}
	// 开路时另一台设备的行只跳过，不增加 attempts，也不写 failure_code。
	other := f.device("IR-S03-SKIP")
	otherRow := f.item(other, 1, time.Now().Add(-2*time.Second), time.Now().Add(-2*time.Second).UnixMilli())
	before := f.calls.Load()
	f.process(otherRow)
	got := f.row(otherRow.ID)
	if got.Attempts != 0 || got.FailureCode != nil || got.ProcessedAt != nil {
		t.Fatalf("open circuit wrote row failure: %+v", got)
	}
	if f.calls.Load() != before {
		t.Fatal("open circuit still queried TD")
	}
	if f.metric("td_missing_circuit_skip") == 0 {
		t.Fatal("skip counter did not increase")
	}
	// 冷却后半开探测命中：恢复并处理其他设备。
	f.expireCooldown()
	f.mode.Store(0)
	f.process(otherRow)
	if !f.success(f.row(otherRow.ID)) {
		t.Fatal("half-open hit did not process skipped device")
	}
	if got := f.metric("td_missing_circuit_recovered"); got != 1 {
		t.Fatalf("recovered counter=%d want 1", got)
	}
	if got := f.state(); got != 0 {
		t.Fatalf("state after recovery=%v", got)
	}
	for _, row := range rows {
		f.retry(row.ID)
		f.process(row)
		if !f.success(f.row(row.ID)) {
			t.Fatalf("device %d did not resume after recovery", row.DeviceID)
		}
	}
}

// 半开 false 重新开路；失败探测设备在另一台设备探测前不得再次抢占名额；
// 另一台设备真实命中才允许恢复。
func TestIRS03HalfOpenFalseRotatesIsolated(t *testing.T) {
	f := irS03FixtureNew(t)
	f.mode.Store(1)
	n := int(tdMissingMinDevices)
	devices := make([]model.Device, n)
	rows := make([]model.Inbox, n)
	for i := range devices {
		devices[i] = f.device(fmt.Sprintf("IR-S03-HALF-%03d", i))
		rows[i] = f.item(devices[i], 1, time.Now().Add(-2*time.Second), time.Now().Add(-2*time.Second).UnixMilli())
	}
	for _, row := range rows {
		f.process(row)
	}
	time.Sleep(15 * time.Millisecond)
	for _, row := range rows {
		f.retry(row.ID)
	}
	for _, row := range rows {
		f.process(row)
	}
	if got := f.state(); got != 1 {
		t.Fatalf("setup did not open: %v", got)
	}
	f.expireCooldown()
	// 半开 false：重新开路。
	f.retry(rows[0].ID)
	f.process(rows[0])
	if got := f.state(); got != 1 {
		t.Fatalf("half-open false state=%v want open", got)
	}
	if got := f.metric("td_missing_circuit_open"); got != 2 {
		t.Fatalf("open counter=%d want 2", got)
	}
	f.expireCooldown()
	before := f.calls.Load()
	f.retry(rows[0].ID)
	f.process(rows[0])
	if got := f.state(); got != 2 {
		t.Fatalf("failed probe device state=%v want half-open", got)
	}
	if f.calls.Load() != before {
		t.Fatal("failed probe device queried TD again before another device probed")
	}
	// 另一台设备命中才恢复。
	f.setMode(rows[1].DeviceID, 0)
	f.retry(rows[1].ID)
	f.process(rows[1])
	if !f.success(f.row(rows[1].ID)) {
		t.Fatal("other device hit did not recover")
	}
	if got := f.metric("td_missing_circuit_recovered"); got != 1 {
		t.Fatalf("recovered counter=%d want 1", got)
	}
	if got := f.state(); got != 0 {
		t.Fatalf("state after rotate recovery=%v", got)
	}
}

// TD 查询错误仍走原错误熔断：不计成熟查无、不开缺失熔断；错误熔断恢复后
// 缺失熔断仍保持关闭。
func TestIRS03ErrorCircuitIsSeparateIsolated(t *testing.T) {
	f := irS03FixtureNew(t)
	f.mode.Store(2)
	d := f.device("IR-S03-ERR")
	row := f.item(d, 1, time.Now().Add(-2*time.Second), time.Now().Add(-2*time.Second).UnixMilli())
	f.process(row)
	row = f.row(row.ID)
	f.retry(row.ID)
	f.process(row)
	row = f.row(row.ID)
	if row.FailureCode == nil || *row.FailureCode != "td_unavailable" {
		t.Fatalf("td error failure code=%+v", row.FailureCode)
	}
	if got := f.metric("td_query_error"); got != 2 {
		t.Fatalf("query error counter=%d want 2", got)
	}
	if got := f.metric("td_missing_mature"); got != 0 {
		t.Fatalf("td error counted as mature: %d", got)
	}
	if got := f.state(); got != 0 {
		t.Fatalf("td error opened missing circuit: %v", got)
	}
	f.s.tdCircuit.mu.Lock()
	failures := f.s.tdCircuit.failures
	f.s.tdCircuit.mu.Unlock()
	if failures < 2 {
		t.Fatalf("error circuit failures=%d want >=2", failures)
	}
	// 错误熔断恢复走原路径；缺失熔断不受影响。
	f.s.tdCircuit.mu.Lock()
	f.s.tdCircuit.until = time.Now().Add(-time.Second)
	f.s.tdCircuit.mu.Unlock()
	f.mode.Store(0)
	f.retry(row.ID)
	f.process(row)
	if !f.success(f.row(row.ID)) {
		t.Fatal("TD error recovery failed")
	}
	if got := f.state(); got != 0 {
		t.Fatalf("missing state after error recovery=%v", got)
	}
}

// 取消/预算截止不是 TD 错误也不是查无：不得累计任何缺失证据，也不得写行失败。
func TestIRS03CancellationNotMissingIsolated(t *testing.T) {
	f := irS03FixtureNew(t)
	d := f.device("IR-S03-CANCEL")
	f.setMode(d.ID, 3) // 该设备查询阻塞，等待父 context 取消
	row := f.item(d, 1, time.Now().Add(-2*time.Second), time.Now().Add(-2*time.Second).UnixMilli())
	f.processCancelDuringQuery(row)
	got := f.row(row.ID)
	if got.Attempts != 0 || got.FailureCode != nil || got.ProcessedAt != nil {
		t.Fatalf("cancellation wrote row failure: %+v", got)
	}
	if f.metric("td_query_error") != 0 || f.metric("td_query_missing") != 0 || f.metric("td_missing_mature") != 0 {
		t.Fatalf("cancellation polluted counters: error=%d missing=%d mature=%d", f.metric("td_query_error"), f.metric("td_query_missing"), f.metric("td_missing_mature"))
	}
	if got := f.state(); got != 0 {
		t.Fatalf("cancellation changed state: %v", got)
	}
	// 取消后重试仍然可以正常命中。
	f.setMode(d.ID, 0)
	f.retry(row.ID)
	f.process(row)
	if !f.success(f.row(row.ID)) {
		t.Fatal("retry after cancellation failed")
	}
}

// 半开探测遇到取消：必须释放唯一探测名额、保持半开、不写行失败或查无证据；
// 随后同一设备真实命中才恢复。覆盖 processInboxItem 中“取消先于错误/查无/命中
// 分支”的接线。
func TestIRS03CancelledHalfOpenProbeReleasedIsolated(t *testing.T) {
	f := irS03FixtureNew(t)
	d := f.device("IR-S03-CANCEL-PROBE")
	f.setMode(d.ID, 3) // 查询阻塞，等待父 context 取消
	row := f.item(d, 1, time.Now().Add(-2*time.Second), time.Now().Add(-2*time.Second).UnixMilli())
	// 从 open 进入 half-open，并让 allowQuery 发放唯一探测名额。
	f.s.tdMissingCircuit.mu.Lock()
	f.s.tdMissingCircuit.state = tdMissingStateOpen
	f.s.tdMissingCircuit.until = time.Now().Add(-time.Second)
	f.s.tdMissingCircuit.mu.Unlock()
	f.processCancelDuringQuery(row)
	got := f.row(row.ID)
	if got.Attempts != 0 || got.FailureCode != nil || got.ProcessedAt != nil {
		t.Fatalf("cancellation wrote row failure: %+v", got)
	}
	f.s.tdMissingCircuit.mu.Lock()
	probe := f.s.tdMissingCircuit.probeInFlight
	state := f.s.tdMissingCircuit.state
	f.s.tdMissingCircuit.mu.Unlock()
	if probe {
		t.Fatal("cancelled half-open probe was not released")
	}
	if state != tdMissingStateHalfOpen {
		t.Fatalf("cancellation changed circuit state: %d", state)
	}
	if f.metric("td_query_error") != 0 || f.metric("td_query_missing") != 0 || f.metric("td_missing_mature") != 0 {
		t.Fatalf("cancellation polluted counters: error=%d missing=%d mature=%d", f.metric("td_query_error"), f.metric("td_query_missing"), f.metric("td_missing_mature"))
	}
	// 重试取得新的半开探测并真实命中，才关闭缺失熔断。
	f.setMode(d.ID, 0)
	f.retry(row.ID)
	f.process(row)
	if !f.success(f.row(row.ID)) {
		t.Fatal("retry after cancelled probe failed")
	}
	if got := f.state(); got != 0 {
		t.Fatalf("state after probe hit=%v want closed", got)
	}
}

// 进程重启后内存熔断从 closed 开始，不继承旧的开路状态；数据库行仍按原语义处理。
func TestIRS03RestartStartsClosedIsolated(t *testing.T) {
	f := irS03FixtureNew(t)
	f.s.tdMissingCircuit.mu.Lock()
	f.s.tdMissingCircuit.state = tdMissingStateOpen
	f.s.tdMissingCircuit.until = time.Now().Add(time.Hour)
	f.s.tdMissingCircuit.mu.Unlock()
	if got := f.state(); got != 1 {
		t.Fatalf("setup state=%v", got)
	}
	cfg := config.Config{
		TDURL:                   f.tdServer.URL,
		TDDatabase:              "ir_s03",
		SimulationAlarmMode:     "off",
		JWTSecret:               strings.Repeat("x", 32),
		TDMissingCircuitEnabled: true,
	}
	restarted := New(cfg, f.db, nil, tdengine.New(cfg), zap.NewNop())
	if got := restarted.tdMissingCircuit.snapshot(time.Now()); got != 0 {
		t.Fatalf("restarted circuit state=%v want closed", got)
	}
	d := f.device("IR-S03-RESTART")
	row := f.item(d, 1, time.Now(), time.Now().Add(-time.Second).UnixMilli())
	restarted.processInboxItem(context.Background(), row)
	if !f.success(f.row(row.ID)) {
		t.Fatal("restarted server could not process hit")
	}
}

// 并发处理多台设备：同一行只提交一次，闭合状态不因并发产生重复/丢失。
func TestIRS03ConcurrentHitsIsolated(t *testing.T) {
	f := irS03FixtureNew(t)
	f.mode.Store(0)
	const n = 8
	rows := make([]model.Inbox, n)
	for i := 0; i < n; i++ {
		d := f.device(fmt.Sprintf("IR-S03-CONC-%03d", i))
		rows[i] = f.item(d, 1, time.Now(), time.Now().Add(-time.Second).UnixMilli())
	}
	var wg sync.WaitGroup
	for i := range rows {
		wg.Add(1)
		go func(row model.Inbox) {
			defer wg.Done()
			f.s.processInboxItem(context.Background(), row)
		}(rows[i])
	}
	wg.Wait()
	for _, row := range rows {
		if !f.success(f.row(row.ID)) {
			t.Fatalf("concurrent row %d not successful", row.ID)
		}
	}
	if got := f.metric("td_query_hit"); got != n {
		t.Fatalf("hit counter=%d want %d", got, n)
	}
	if got := f.state(); got != 0 {
		t.Fatalf("concurrent hits changed state=%v", got)
	}
}

// 与 S02 过期出口共存：8 天旧行按 window_expired 落终态，owner_abandoned 不动；
// 近期 pending 行仍可累计成熟缺失，过期行不会成为缺失证据。
func TestIRS03CoexistsWithS02ExpiryIsolated(t *testing.T) {
	f := irS03FixtureNew(t)
	now := time.Now()
	oldTS := now.Add(-8 * 24 * time.Hour).UnixMilli()
	oldDevice := f.device("IR-S03-OLD")
	oldRow := f.item(oldDevice, 1, now, oldTS)
	abandoned := f.device("IR-S03-ABANDONED")
	abandonedRow := f.item(abandoned, 1, now, oldTS)
	if err := f.db.Exec(`UPDATE telemetry_inbox SET business_state='quarantined',processed_at=now(),failure_code='owner_abandoned' WHERE id=?`, abandonedRow.ID).Error; err != nil {
		t.Fatal(err)
	}
	recent := f.device("IR-S03-RECENT")
	recentRow := f.item(recent, 1, now.Add(-2*time.Second), now.UnixMilli())

	floor := now.Add(-inboxWindow).UnixMilli()
	changed, err := f.s.expireInboxBatch(context.Background(), floor, 100)
	if err != nil || changed != 1 {
		t.Fatalf("expiry changed=%d err=%v want 1", changed, err)
	}
	old := f.row(oldRow.ID)
	if old.ProcessedAt == nil || old.BusinessState == nil || *old.BusinessState != "quarantined" || old.FailureCode == nil || *old.FailureCode != "window_expired" {
		t.Fatalf("old row not window_expired: %+v", old)
	}
	kept := f.row(abandonedRow.ID)
	if kept.FailureCode == nil || *kept.FailureCode != "owner_abandoned" {
		t.Fatalf("owner_abandoned changed: %+v", kept)
	}
	f.mode.Store(1)
	f.process(recentRow)
	f.retry(recentRow.ID)
	f.process(recentRow)
	if got := f.metric("td_missing_mature"); got != 2 {
		t.Fatalf("mature counter=%d want 2 (expired rows must not count)", got)
	}
	if got := f.state(); got != 0 {
		t.Fatalf("expired rows influenced circuit: %v", got)
	}
}
