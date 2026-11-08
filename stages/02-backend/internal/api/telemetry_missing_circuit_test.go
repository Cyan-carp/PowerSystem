package api

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	"powersystem/backend/internal/config"
)

func tdMissingTestCircuit() *tdMissingCircuit {
	return &tdMissingCircuit{enabled: true, settings: defaultTDMissingCircuitSettings()}
}

func TestTDMissingCircuitDefaultsAreFrozen(t *testing.T) {
	want := tdMissingCircuitSettings{
		MatureAge:       2 * time.Minute,
		EvidenceWindow:  3 * time.Minute,
		MinDeviceChecks: 2,
		ConfirmSpan:     30 * time.Second,
		MinDevices:      16,
		Cooldown:        30 * time.Second,
		EvidenceLimit:   1024,
	}
	if got := defaultTDMissingCircuitSettings(); got != want {
		t.Fatalf("冻结值漂移: got %+v want %+v", got, want)
	}
	if got := (tdMissingCircuitSettings{}).withDefaults(); got != want {
		t.Fatalf("零值设置未回落到冻结值: %+v", got)
	}
}

func TestTDMissingCircuitIgnoresFreshFalse(t *testing.T) {
	c := tdMissingTestCircuit()
	base := time.Unix(1_700_000_000, 0)
	// 20 台设备连续收到近期 false。它们只能算 td_query_missing，不能算成熟证据。
	for id := int64(1); id <= 20; id++ {
		if mature, opened := c.observeMissing(base, id, base, false); mature || opened {
			t.Fatalf("device %d: fresh false became mature/open", id)
		}
		if mature, opened := c.observeMissing(base.Add(90*time.Second), id, base, false); mature || opened {
			t.Fatalf("device %d: 90s false became mature/open (mature=2m)", id)
		}
	}
	if got := c.snapshot(base.Add(90 * time.Second)); got != 0 {
		t.Fatalf("fresh false opened circuit: state=%v", got)
	}
	// 随后真实命中也不能产生 recovered 转换（电路本来就关闭）。
	if c.observeHit(base.Add(100*time.Second), 1) {
		t.Fatal("closed circuit reported recovered")
	}
}

func TestTDMissingCircuitSingleDeviceHoleNeverOpens(t *testing.T) {
	c := tdMissingTestCircuit()
	base := time.Unix(1_700_000_000, 0)
	// 一台设备持续 2 小时、每 10 秒一次成熟查无：证据再多也只能算一个设备。
	for i := 0; i < 720; i++ {
		now := base.Add(time.Duration(i) * 10 * time.Second)
		if _, opened := c.observeMissing(now, 1, base, false); opened {
			t.Fatalf("single-device hole opened global circuit at %s", now.Sub(base))
		}
		if got := c.snapshot(now); got != 0 {
			t.Fatalf("single-device hole state=%v", got)
		}
	}
	c.mu.Lock()
	n := len(c.evidence)
	c.mu.Unlock()
	if n != 1 {
		t.Fatalf("single device produced %d evidence entries", n)
	}
}

func TestTDMissingCircuitRequiresCrossDevicePersistence(t *testing.T) {
	base := time.Unix(1_700_000_000, 0)
	// 两台设备各自成熟且持续，仍不足以开全局。
	two := tdMissingTestCircuit()
	for round := 0; round < 4; round++ {
		now := base.Add(2*time.Minute + time.Duration(round)*31*time.Second)
		for id := int64(1); id <= 2; id++ {
			if _, opened := two.observeMissing(now, id, base, false); opened {
				t.Fatal("two devices opened global circuit")
			}
		}
	}
	// 每台设备各一次成熟查无：设备数够但没有持续证据，不开。
	minDevices := int64(tdMissingMinDevices)
	once := tdMissingTestCircuit()
	first := base.Add(2 * time.Minute)
	for id := int64(1); id <= minDevices; id++ {
		if _, opened := once.observeMissing(first, id, base, false); opened {
			t.Fatal("single mature observation per device opened circuit")
		}
	}
	// 达到门槛的设备各两次成熟查无且跨度 >= 30s：最后一台第二次观测时开路。
	many := tdMissingTestCircuit()
	for id := int64(1); id <= minDevices; id++ {
		if _, opened := many.observeMissing(first, id, base, false); opened {
			t.Fatal("first round opened circuit")
		}
	}
	second := first.Add(31 * time.Second)
	for id := int64(1); id <= minDevices; id++ {
		_, opened := many.observeMissing(second, id, base, false)
		if id < minDevices && opened {
			t.Fatalf("circuit opened before threshold device, id=%d", id)
		}
		if id == minDevices && !opened {
			t.Fatal("cross-device persistent missing did not open circuit")
		}
	}
	if got := many.snapshot(second); got != 1 {
		t.Fatalf("state after open=%v want 1", got)
	}
	if got := many.snapshot(second.Add(30 * time.Second)); got != 2 {
		t.Fatalf("state after cooldown=%v want 2", got)
	}
}

func TestTDMissingCircuitHalfOpenFalseRotatesAndHitRecovers(t *testing.T) {
	c := tdMissingTestCircuit()
	base := time.Unix(1_700_000_000, 0)
	c.mu.Lock()
	c.state = tdMissingStateOpen
	c.until = base
	c.mu.Unlock()

	first := c.allowQuery(base, 1, base.Add(-3*time.Minute))
	if !first.Allow || !first.Probe || !first.HalfOpenTransition {
		t.Fatalf("open->half-open not granted: %+v", first)
	}
	if other := c.allowQuery(base, 2, base.Add(-3*time.Minute)); other.Allow {
		t.Fatalf("second probe allowed while one in flight: %+v", other)
	}
	// false 探测：重新开路，并记住失败设备。
	mature, opened := c.observeMissing(base, 1, base.Add(-3*time.Minute), true)
	if !mature || !opened {
		t.Fatalf("probe false mature=%v opened=%v", mature, opened)
	}
	if got := c.snapshot(base); got != 1 {
		t.Fatalf("probe false state=%v want open", got)
	}
	if d := c.allowQuery(base.Add(29*time.Second), 2, base.Add(-3*time.Minute)); d.Allow {
		t.Fatal("probe allowed before cooldown")
	}
	// 冷却结束后不得再让失败设备抢占探测；另一台设备可以拿到名额。
	same := c.allowQuery(base.Add(30*time.Second), 1, base.Add(-3*time.Minute))
	if same.Allow || !same.HalfOpenTransition {
		t.Fatalf("failed probe device got another probe: %+v", same)
	}
	next := c.allowQuery(base.Add(30*time.Second), 2, base.Add(-3*time.Minute))
	if !next.Allow || !next.Probe {
		t.Fatalf("other device did not get probe: %+v", next)
	}
	// 只有真实命中才恢复。
	if !c.observeHit(base.Add(31*time.Second), 2) {
		t.Fatal("half-open hit did not recover")
	}
	if got := c.snapshot(base.Add(31 * time.Second)); got != 0 {
		t.Fatalf("state after hit=%v want closed", got)
	}
}

func TestTDMissingCircuitErrorIsNotMissingEvidence(t *testing.T) {
	c := tdMissingTestCircuit()
	base := time.Unix(1_700_000_000, 0)
	// 连续查询错误：既不累计成熟证据，也不开缺失熔断。
	for id := int64(1); id <= 20; id++ {
		if opened := c.observeError(base, id, false); opened {
			t.Fatalf("error opened missing circuit, device %d", id)
		}
	}
	if got := c.snapshot(base); got != 0 {
		t.Fatalf("errors changed missing state: %v", got)
	}
	// 半开探测错误：释放名额并重新开路，但不产生成熟计数。
	c.mu.Lock()
	c.state = tdMissingStateOpen
	c.until = base
	c.mu.Unlock()
	probe := c.allowQuery(base, 9, base.Add(-3*time.Minute))
	if !probe.Allow || !probe.Probe {
		t.Fatalf("probe not granted: %+v", probe)
	}
	if !c.observeError(base, 9, probe.Probe) {
		t.Fatal("half-open error did not reopen")
	}
	if got := c.snapshot(base); got != 1 {
		t.Fatalf("state after probe error=%v want open", got)
	}
}

func TestTDMissingCircuitDisabledNeverOpens(t *testing.T) {
	c := &tdMissingCircuit{}
	base := time.Unix(1_700_000_000, 0)
	for id := int64(1); id <= 20; id++ {
		for round := 0; round < 3; round++ {
			now := base.Add(2*time.Minute + time.Duration(round)*31*time.Second)
			mature, opened := c.observeMissing(now, id, base, false)
			if !mature || opened {
				t.Fatalf("disabled circuit mature=%v opened=%v", mature, opened)
			}
		}
	}
	if got := c.snapshot(base.Add(5 * time.Minute)); got != 0 {
		t.Fatalf("disabled circuit state=%v", got)
	}
	// 即使内部状态被异常置为 open，disabled 也必须无条件放行。
	c.mu.Lock()
	c.state = tdMissingStateOpen
	c.until = base.Add(-time.Hour)
	c.mu.Unlock()
	if d := c.allowQuery(base, 1, base); !d.Allow {
		t.Fatal("disabled circuit denied query")
	}
}

func TestTDMissingCircuitHalfOpenProbeRequiresMatureRow(t *testing.T) {
	c := tdMissingTestCircuit()
	base := time.Unix(1_700_000_000, 0)
	c.mu.Lock()
	c.state = tdMissingStateOpen
	c.until = base
	c.mu.Unlock()
	// 近期 false 的样本不能作为探测：先留在半开，等成熟样本。
	recent := c.allowQuery(base, 1, base)
	if recent.Allow || !recent.HalfOpenTransition {
		t.Fatalf("recent row granted half-open probe: %+v", recent)
	}
	mature := c.allowQuery(base, 2, base.Add(-3*time.Minute))
	if !mature.Allow || !mature.Probe {
		t.Fatalf("mature row did not get probe: %+v", mature)
	}
}

func TestTDMissingCircuitHalfOpenAllowsExactlyOneProbe(t *testing.T) {
	c := tdMissingTestCircuit()
	base := time.Unix(1_700_000_000, 0)
	c.mu.Lock()
	c.state = tdMissingStateOpen
	c.until = base
	c.mu.Unlock()
	var wg sync.WaitGroup
	results := make(chan bool, 64)
	for i := 0; i < 64; i++ {
		wg.Add(1)
		go func(id int64) {
			defer wg.Done()
			results <- c.allowQuery(base, id, base.Add(-3*time.Minute)).Probe
		}(int64(i + 1))
	}
	wg.Wait()
	close(results)
	probes := 0
	for p := range results {
		if p {
			probes++
		}
	}
	if probes != 1 {
		t.Fatalf("half-open granted %d probes, want 1", probes)
	}
}

func TestNewWiresTDMissingCircuitFlag(t *testing.T) {
	off := New(config.Config{}, nil, nil, nil, nil)
	if off.tdMissingCircuit.enabled {
		t.Fatal("TD_MISSING_CIRCUIT_ENABLED must default to false")
	}
	on := New(config.Config{TDMissingCircuitEnabled: true}, nil, nil, nil, nil)
	if !on.tdMissingCircuit.enabled {
		t.Fatal("explicit flag did not enable circuit")
	}
}

// /metrics 的锁序是 circuit -> telemetry；核心若在持 circuit 锁时反向拿
// telemetry 锁就会死锁。此测试先占住 telemetry 锁，核心方法必须仍能完成。
func TestTDMissingCircuitDoesNotTakeTelemetryLock(t *testing.T) {
	s := &Server{}
	s.tdMissingCircuit = *tdMissingTestCircuit()
	base := time.Unix(1_700_000_000, 0)
	s.telemetry.mu.Lock()
	done := make(chan struct{})
	go func() {
		defer close(done)
		s.tdMissingCircuit.allowQuery(base, 1, base.Add(-3*time.Minute))
		s.tdMissingCircuit.observeMissing(base, 1, base.Add(-3*time.Minute), false)
		s.tdMissingCircuit.observeHit(base, 1)
		_ = s.tdMissingCircuit.snapshot(base)
	}()
	select {
	case <-done:
		s.telemetry.mu.Unlock()
	case <-time.After(time.Second):
		s.telemetry.mu.Unlock()
		t.Fatal("circuit method blocked on telemetry lock: lock order regression")
	}
}

// 回归：FirstSeen 滑出证据窗后必须重置该设备的连续证据。旧实现只按 LastSeen
// 清旧设备，只要中间再来一次查无，数小时前的 FirstSeen/Checks 就会被保留，
// 让跨窗的稀疏查无拼成“持续”。重置后同一设备在窗口内真正连续查无仍可开路。
func TestTDMissingCircuitEvidenceWindowIsBounded(t *testing.T) {
	c := &tdMissingCircuit{enabled: true, settings: tdMissingCircuitSettings{
		MatureAge:       time.Minute,
		EvidenceWindow:  3 * time.Minute,
		MinDeviceChecks: 3,
		ConfirmSpan:     30 * time.Second,
		MinDevices:      2,
		Cooldown:        30 * time.Second,
		EvidenceLimit:   8,
	}}
	base := time.Unix(1_700_000_000, 0)
	received := base.Add(-2 * time.Minute)
	// 两台设备在 base 与 base+2m 各有一次成熟查无。base+2m 时 base 的观测
	// 仍在窗内，旧实现会把 LastSeen 续到 base+2m、Checks 累计到 2。
	for id := int64(1); id <= 2; id++ {
		if _, opened := c.observeMissing(base, id, received, false); opened {
			t.Fatalf("device %d opened at first check", id)
		}
		if _, opened := c.observeMissing(base.Add(2*time.Minute), id, received, false); opened {
			t.Fatalf("device %d opened before MinDeviceChecks", id)
		}
	}
	// base+4m 距 base 已超过 3 分钟。旧实现保留 FirstSeen=base、Checks=3，
	// 会把两台设备算成持续证据并开路；窗口锚定 FirstSeen 后必须从本次观测
	// 重新起算，不能与旧查无拼接。
	stale := base.Add(4 * time.Minute)
	for id := int64(1); id <= 2; id++ {
		if _, opened := c.observeMissing(stale, id, received, false); opened {
			t.Fatalf("device %d: stale checks across window combined into persistence", id)
		}
	}
	c.mu.Lock()
	for id := int64(1); id <= 2; id++ {
		ev := c.evidence[id]
		if ev == nil {
			c.mu.Unlock()
			t.Fatalf("device %d evidence missing after window reset", id)
		}
		if ev.Checks != 1 || !ev.FirstSeen.Equal(stale) {
			c.mu.Unlock()
			t.Fatalf("device %d evidence not reset at window: %+v", id, *ev)
		}
	}
	c.mu.Unlock()
	// 同一窗口内真正连续的查无仍可开路：stale、stale+31s、stale+62s 都在
	// 3 分钟窗内，第三次观测满足 3 次/30 秒跨度，最后一台设备触发开路。
	for _, at := range []time.Time{stale.Add(31 * time.Second), stale.Add(62 * time.Second)} {
		for id := int64(1); id <= 2; id++ {
			_, opened := c.observeMissing(at, id, received, false)
			if at.Equal(stale.Add(31*time.Second)) && opened {
				t.Fatalf("device %d opened at second check", id)
			}
			if at.Equal(stale.Add(62*time.Second)) && id < 2 && opened {
				t.Fatalf("device %d opened before last device", id)
			}
			if at.Equal(stale.Add(62*time.Second)) && id == 2 && !opened {
				t.Fatal("continuous in-window evidence did not open circuit")
			}
		}
	}
	if got := c.snapshot(stale.Add(62 * time.Second)); got != 1 {
		t.Fatalf("state after continuous in-window evidence=%v want open", got)
	}
}

// 边界等号：窗口为闭区间，FirstSeen 恰好等于 EvidenceWindow 时仍参与判定；
// 超出 1ns 才重置。这里用中间观测保持 LastSeen 新鲜，确保走到 FirstSeen 重置
// 分支而不是整条证据被清理。
func TestTDMissingCircuitEvidenceWindowBoundary(t *testing.T) {
	settings := tdMissingCircuitSettings{
		MatureAge:       time.Minute,
		EvidenceWindow:  3 * time.Minute,
		MinDeviceChecks: 3,
		ConfirmSpan:     30 * time.Second,
		MinDevices:      2,
		Cooldown:        30 * time.Second,
		EvidenceLimit:   8,
	}
	base := time.Unix(1_700_000_000, 0)
	received := base.Add(-2 * time.Minute)
	seed := func(c *tdMissingCircuit) {
		for id := int64(1); id <= 2; id++ {
			c.observeMissing(base, id, received, false)
			c.observeMissing(base.Add(time.Minute), id, received, false)
		}
	}
	// 恰好 +3m：FirstSeen 未超过窗口，第三次观测仍可计数并开路。
	edge := &tdMissingCircuit{enabled: true, settings: settings}
	seed(edge)
	atEdge := base.Add(3 * time.Minute)
	if _, opened := edge.observeMissing(atEdge, 1, received, false); opened {
		t.Fatal("edge opened before last device")
	}
	if _, opened := edge.observeMissing(atEdge, 2, received, false); !opened {
		t.Fatal("exact window edge did not count as continuous evidence")
	}
	// +3m+1ns：超过窗口，重置后每个设备只有本次一条证据。
	past := &tdMissingCircuit{enabled: true, settings: settings}
	seed(past)
	atPast := base.Add(3*time.Minute + time.Nanosecond)
	for id := int64(1); id <= 2; id++ {
		if _, opened := past.observeMissing(atPast, id, received, false); opened {
			t.Fatalf("device %d: check past window edge counted", id)
		}
	}
	if got := past.snapshot(atPast); got != 0 {
		t.Fatalf("state past window edge=%v want closed", got)
	}
}

// 证据表仍须有固定上限，重置逻辑不能让其按设备无限扩容。
func TestTDMissingCircuitEvidenceLimitIsBounded(t *testing.T) {
	c := &tdMissingCircuit{enabled: true, settings: tdMissingCircuitSettings{
		MatureAge:       time.Minute,
		EvidenceWindow:  3 * time.Minute,
		MinDeviceChecks: 2,
		ConfirmSpan:     30 * time.Second,
		MinDevices:      16,
		Cooldown:        30 * time.Second,
		EvidenceLimit:   3,
	}}
	base := time.Unix(1_700_000_000, 0)
	received := base.Add(-2 * time.Minute)
	for id := int64(1); id <= 10; id++ {
		c.observeMissing(base.Add(time.Duration(id)*time.Second), id, received, false)
	}
	c.mu.Lock()
	n := len(c.evidence)
	c.mu.Unlock()
	if n != 3 {
		t.Fatalf("evidence entries=%d want 3 (bounded by EvidenceLimit)", n)
	}
}

// HasSample 返回 (false,nil) 恰遇父/tx context 取消时，必须 fail-closed：
// 释放半开探测名额并返回可回滚的 context 错误；不能把取消当成查无。
func TestTDQueryCancellationReleasesProbeFailClosed(t *testing.T) {
	s := &Server{}
	s.tdMissingCircuit = *tdMissingTestCircuit()
	s.tdMissingCircuit.mu.Lock()
	s.tdMissingCircuit.state = tdMissingStateHalfOpen
	s.tdMissingCircuit.probeInFlight = true
	s.tdMissingCircuit.mu.Unlock()

	live := context.Background()
	txctx, txcancel := context.WithCancel(context.Background())
	defer txcancel()
	if err := s.tdQueryCancellationError(live, txctx, nil, true); err != nil {
		t.Fatalf("live contexts reported cancellation: %v", err)
	}
	s.tdMissingCircuit.mu.Lock()
	probe := s.tdMissingCircuit.probeInFlight
	s.tdMissingCircuit.mu.Unlock()
	if !probe {
		t.Fatal("live contexts released probe")
	}

	parent, parentCancel := context.WithCancel(context.Background())
	parentCancel()
	if err := s.tdQueryCancellationError(parent, txctx, nil, true); !errors.Is(err, context.Canceled) {
		t.Fatalf("parent cancellation not fail-closed: %v", err)
	}
	s.tdMissingCircuit.mu.Lock()
	probe = s.tdMissingCircuit.probeInFlight
	state := s.tdMissingCircuit.state
	s.tdMissingCircuit.mu.Unlock()
	if probe {
		t.Fatal("parent cancellation did not release probe")
	}
	if state != tdMissingStateHalfOpen {
		t.Fatalf("cancellation changed circuit state: %d", state)
	}

	// txctx 取消同样释放名额并返回 context 错误。
	s.tdMissingCircuit.mu.Lock()
	s.tdMissingCircuit.probeInFlight = true
	s.tdMissingCircuit.mu.Unlock()
	txcancel()
	if err := s.tdQueryCancellationError(live, txctx, nil, true); !errors.Is(err, context.Canceled) {
		t.Fatalf("txctx cancellation not fail-closed: %v", err)
	}
	s.tdMissingCircuit.mu.Lock()
	probe = s.tdMissingCircuit.probeInFlight
	s.tdMissingCircuit.mu.Unlock()
	if probe {
		t.Fatal("txctx cancellation did not release probe")
	}
}

func TestTDQueryDependencyDeadlineSkipsWithoutMissingEvidence(t *testing.T) {
	s := &Server{}
	s.tdMissingCircuit = *tdMissingTestCircuit()
	s.tdMissingCircuit.mu.Lock()
	s.tdMissingCircuit.state = tdMissingStateHalfOpen
	s.tdMissingCircuit.probeInFlight = true
	s.tdMissingCircuit.mu.Unlock()
	if err := s.tdQueryCancellationError(context.Background(), context.Background(), context.DeadlineExceeded, true); !errors.Is(err, errDependencyOpen) {
		t.Fatalf("dependency deadline should skip attempt, got %v", err)
	}
	s.tdMissingCircuit.mu.Lock()
	probe := s.tdMissingCircuit.probeInFlight
	state := s.tdMissingCircuit.state
	s.tdMissingCircuit.mu.Unlock()
	if probe || state != tdMissingStateHalfOpen {
		t.Fatalf("deadline must release probe without changing state: probe=%v state=%d", probe, state)
	}
}
