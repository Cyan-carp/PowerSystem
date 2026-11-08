package api

import (
	"math"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
)

// 过期出口必须与调度共用 ts_ms 时间窗，不能改用 received_at；否则“旧 ts、新接收”
// 的补传行永远不会过期，IR-R01 会原样复现。方向也必须保持 id ASC，不能重演
// 2026-11-03 的取任务/复核漂移。
func TestInboxExpiryQueryKeepsSharedWindowAndOwnerGuard(t *testing.T) {
	q := buildInboxExpiryQuery()
	if !strings.Contains(q, inboxExpiryPredicate) {
		t.Fatalf("过期候选未复用共享时间窗谓词: %s", q)
	}
	if strings.Contains(q, "received_at<") || strings.Contains(q, "received_at <") {
		t.Fatalf("过期判据不得按 received_at: %s", q)
	}
	if !strings.Contains(q, "ORDER BY id ASC") || strings.Contains(q, "id DESC") {
		t.Fatalf("过期候选必须保持 id ASC: %s", q)
	}
	if !strings.Contains(q, "FOR UPDATE SKIP LOCKED") {
		t.Fatalf("过期批次必须跳过正在入库的行，不能阻塞实时入库: %s", q)
	}
	if !strings.Contains(q, "business_state='quarantined'") || !strings.Contains(q, "failure_code='window_expired'") {
		t.Fatalf("过期终态必须为 quarantined/window_expired: %s", q)
	}
	if !strings.Contains(q, "processed_at=clock_timestamp()") {
		t.Fatalf("过期终态必须写 processed_at: %s", q)
	}
	if strings.Contains(q, "DELETE") || strings.Contains(q, "VACUUM") {
		t.Fatalf("过期出口不得删除数据: %s", q)
	}
	// 历史字段必须原样保留：只允许更新终态与调度字段。
	for _, forbidden := range []string{"payload=", "attempts=", "first_failed_at=", "reason_since=", "reason_attempts=", "missing_since=", "missing_attempts=", "missing_checked_at=", "td_confirmed_at="} {
		if strings.Contains(q, forbidden) {
			t.Fatalf("过期出口不得改写历史字段 %s: %s", forbidden, q)
		}
	}
	// UPDATE 侧也要有独立防护，不能只依赖 CTE 的 SELECT。
	if got := strings.Count(q, "failure_code IS DISTINCT FROM 'owner_abandoned'"); got != 2 {
		t.Fatalf("SELECT 与 UPDATE 都应带 owner_abandoned 防护，实际 %d 处: %s", got, q)
	}
	if got := strings.Count(q, "processed_at IS NULL"); got < 2 {
		t.Fatalf("SELECT 与 UPDATE 都应带 processed_at IS NULL 防护，实际 %d 处: %s", got, q)
	}
	// history_enabled 是操作者暂停开关；SELECT 与 UPDATE 都要 fail-closed。
	if !strings.Contains(q, "history_enabled") {
		t.Fatalf("过期出口缺少 history_enabled 门禁: %s", q)
	}
	if got := strings.Count(q, "EXISTS (SELECT 1 FROM gate)"); got != 2 {
		t.Fatalf("SELECT 与 UPDATE 都应引用 history_enabled 门禁，实际 %d 处: %s", got, q)
	}
	if strings.Contains(q, "history_enabled IS NULL") || strings.Contains(q, "history_enabled<>false") {
		t.Fatalf("history_enabled 门禁不得把缺失/暂停写成放行: %s", q)
	}
}

// 队头年龄必须是“每设备窗口内最旧 pending 一条”，不是全局最旧 200 行样本。
func TestInboxHeadAgeQueryIsPerDeviceInWindow(t *testing.T) {
	q := inboxHeadAgeQuery
	for _, want := range []string{
		"FROM devices WHERE deleted_at IS NULL",
		"CROSS JOIN LATERAL",
		"i.device_id=dev.id",
		"i.processed_at IS NULL",
		"i.ts_ms>=?",
		"ORDER BY i.id ASC LIMIT 1",
		"count(*) AS devices",
	} {
		if !strings.Contains(q, want) {
			t.Fatalf("队头查询缺少 %q: %s", want, q)
		}
	}
	if strings.Contains(q, "LIMIT 200") || strings.Contains(q, "SELECT id FROM telemetry_inbox WHERE processed_at IS NULL ORDER BY id ASC LIMIT ?") {
		t.Fatalf("队头查询不得退回全局最旧样本: %s", q)
	}
	if strings.Contains(q, "i.ts_ms<?") || strings.Contains(q, "ts_ms < ?") {
		t.Fatalf("队头查询不得把窗口外行计入: %s", q)
	}
}

func TestInboxExpiryDefaultsAreFrozen(t *testing.T) {
	if inboxExpiryInterval != 30*time.Second || inboxExpiryBatch != 2000 || inboxExpiryBudget != 5*time.Second {
		t.Fatalf("冻结值漂移: interval=%s batch=%d budget=%s", inboxExpiryInterval, inboxExpiryBatch, inboxExpiryBudget)
	}
	got := inboxExpirySettings{}.withDefaults()
	if got.Interval != inboxExpiryInterval || got.Batch != inboxExpiryBatch || got.Budget != inboxExpiryBudget {
		t.Fatalf("默认参数未落到冻结值: %+v", got)
	}
	if got.Enabled {
		t.Fatal("过期写路径默认必须关闭")
	}
	override := inboxExpirySettings{Enabled: true, Interval: 5 * time.Second, Batch: 7, Budget: time.Second}.withDefaults()
	if !override.Enabled || override.Interval != 5*time.Second || override.Batch != 7 || override.Budget != time.Second {
		t.Fatalf("显式覆盖被忽略: %+v", override)
	}
}

func TestServeTelemetryMetricsEmitsS02QueueAndExpiry(t *testing.T) {
	gin.SetMode(gin.TestMode)
	s := &Server{}
	s.telemetry.values = map[string]float64{"probe_pg_ok": 1}
	s.telemetry.auxValues = map[string]float64{
		"inbox_pending_rows_outside_window": 3,
		"inbox_queue_stats_known":           1,
	}
	s.telemetry.counts = map[string]uint64{"expiry_rows": 7, "expiry_paused": 2}
	now := time.Now()
	s.telemetry.times = map[string]time.Time{"expiry_rows": now, "expiry_paused": now, "inbox_queue": now}
	w := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(w)
	c.Request = httptest.NewRequest("GET", "/metrics", nil)
	s.serveTelemetryMetrics(c)
	body := w.Body.String()
	for _, want := range []string{
		"powersystem_telemetry_inbox_pending_rows_outside_window 3",
		"powersystem_telemetry_inbox_queue_stats_known 1",
		"powersystem_telemetry_expiry_rows_total 7",
		"powersystem_telemetry_expiry_paused_total 2",
		`powersystem_telemetry_path_seconds_count{path="expiry"} 0`,
	} {
		if !strings.Contains(body, want) {
			t.Fatalf("metrics body missing %q:\n%s", want, body)
		}
	}
}

func TestInboxQueueUnknownValues(t *testing.T) {
	v := unknownInboxQueueValues()
	if v["inbox_queue_stats_known"] != 0 {
		t.Fatal("未知态必须标记 stats_known=0")
	}
	if v["inbox_head_age_device_limit"] != float64(inboxHeadAgeDeviceLimit) {
		t.Fatal("设备上限必须随指标导出")
	}
	for _, key := range []string{
		"inbox_pending_rows_all", "inbox_pending_rows_in_window", "inbox_pending_rows_outside_window",
		"inbox_outside_window_oldest_age_seconds", "inbox_head_age_devices_total",
		"inbox_head_age_devices_lt_1m", "inbox_head_age_devices_ge_6h",
	} {
		if !math.IsNaN(v[key]) {
			t.Fatalf("%s 未知态应为 NaN，实际 %v", key, v[key])
		}
	}
}
