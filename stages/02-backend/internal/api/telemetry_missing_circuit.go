package api

import (
	"sync"
	"time"
)

// IR-S03：TD「查询成功但没有这一行」的系统性缺失熔断。
//
// HasSample 在 SQL 成功而结果为空时返回 (false,nil)，这与查询报错是两件事：
//   - err != nil 由既有 tdCircuit 负责，按错误次数开路；
//   - false,nil 可能是 TD 尚未写完（96 的 2,230 条假缺失），也可能真是整段
//     TD 写入缺失。本熔断只处理后者，且必须与错误熔断完全分离。
//
// 设计边界：
//   - 近期接收的 false 一律不算证据：只有 received_at 已超过 MatureAge 的查无
//     才进入成熟证据；
//   - 单一设备的永久空洞不得开全局：需要跨 MinDevices 台设备、每台至少
//     MinDeviceChecks 次成熟观测且跨度 >= ConfirmSpan；
//   - 开路后只限制 TD 确认尝试频率，不伪造业务成功，不越过同设备未终态队头；
//   - 冷却结束进入半开，只允许一个真实 HasSample 探测；false/error 重新开路，
//     只有 found==true 才恢复；
//   - 证据表固定上限，不按设备打指标标签；所有方法持本结构锁，但绝不在持锁
//     时调用 telemetry.add/touch，避免与 /metrics 的锁序相反。
const (
	// 一条样本从 inbox 接收到 TD 可查的延迟没有生产实测分布。96 的反证只能
	// 证明“近期 false 不能当真”，不能给出上界。2 分钟远大于单次 TD 查询的
	// 1 秒预算，也小于单行 10 分钟宽限，先作为保守成熟线；生产启用前仍应按
	// 真实延迟重新校准（本阶段没有生产故障注入授权）。
	tdMissingMatureAge = 2 * time.Minute
	// 成熟证据只在最近窗口内有效：每台设备的连续证据以 FirstSeen 为锚，
	// FirstSeen 一旦滑出窗口就从本次观测重新起算，避免把几小时前的偶发查无
	// 拼成“持续”。窗口边界取闭区间：恰好等于 EvidenceWindow 仍算窗内。
	tdMissingEvidenceWindow = 3 * time.Minute
	// 每台设备至少两次成熟观测，且两次跨度 >= ConfirmSpan，证明不是单次抖动。
	tdMissingMinDeviceChecks = 2
	tdMissingConfirmSpan     = 30 * time.Second
	// 全局开路至少需要 16 台不同设备的成熟持续证据（当前平台 143 台，约 11%）。
	// 单设备/少数坏点永远不够，避免几台永久空洞周期性拖停整个车队；真实 TD
	// 写入系统性停摆会在数分钟内覆盖全部活跃设备。站级（约 35 台）仍会触发，
	// 误开成本由 30 秒冷却和真实命中半开恢复限制；舰队规模明显变化须重新校准。
	tdMissingMinDevices = 16
	// 冷却后只放行一个探测。false/error 重新开路，避免把 TD 查询打成重试风暴。
	tdMissingCooldown = 30 * time.Second
	// 证据表固定上限（当前平台 143 台），不随设备数无限增长，也不导出设备标签。
	tdMissingEvidenceLimit = 1024
)

const (
	tdMissingStateClosed = iota
	tdMissingStateOpen
	tdMissingStateHalfOpen
)

// tdMissingCircuitSettings 是冻结值的可测试副本。生产默认值只来自上面的常量；
// 测试可以显式覆盖小窗口，避免用真实时间等待 2 分钟。零值经 withDefaults 回落到
// 冻结值，因此 Server{} 的零值电路仍然安全（默认 disabled）。
type tdMissingCircuitSettings struct {
	MatureAge       time.Duration
	EvidenceWindow  time.Duration
	MinDeviceChecks int
	ConfirmSpan     time.Duration
	MinDevices      int
	Cooldown        time.Duration
	EvidenceLimit   int
}

func defaultTDMissingCircuitSettings() tdMissingCircuitSettings {
	return tdMissingCircuitSettings{
		MatureAge:       tdMissingMatureAge,
		EvidenceWindow:  tdMissingEvidenceWindow,
		MinDeviceChecks: tdMissingMinDeviceChecks,
		ConfirmSpan:     tdMissingConfirmSpan,
		MinDevices:      tdMissingMinDevices,
		Cooldown:        tdMissingCooldown,
		EvidenceLimit:   tdMissingEvidenceLimit,
	}
}

func (v tdMissingCircuitSettings) withDefaults() tdMissingCircuitSettings {
	if v.MatureAge <= 0 {
		v.MatureAge = tdMissingMatureAge
	}
	if v.EvidenceWindow <= 0 {
		v.EvidenceWindow = tdMissingEvidenceWindow
	}
	if v.MinDeviceChecks <= 0 {
		v.MinDeviceChecks = tdMissingMinDeviceChecks
	}
	if v.ConfirmSpan <= 0 {
		v.ConfirmSpan = tdMissingConfirmSpan
	}
	if v.MinDevices <= 0 {
		v.MinDevices = tdMissingMinDevices
	}
	if v.Cooldown <= 0 {
		v.Cooldown = tdMissingCooldown
	}
	if v.EvidenceLimit <= 0 {
		v.EvidenceLimit = tdMissingEvidenceLimit
	}
	return v
}

type tdMissingDeviceEvidence struct {
	FirstSeen time.Time
	LastSeen  time.Time
	Checks    int
}

// tdMissingQueryDecision 由 allowQuery 返回，调用方在锁外记录指标。
type tdMissingQueryDecision struct {
	Allow              bool
	Probe              bool
	HalfOpenTransition bool
}

type tdMissingCircuit struct {
	mu       sync.Mutex
	enabled  bool
	settings tdMissingCircuitSettings

	state         int
	until         time.Time
	probeInFlight bool
	// 上一次失败的半开探测设备。冷却结束后同一台设备不得再次抢占探测，
	// 否则一个永久空洞会把全局熔断钉死在半开→失败→半开的循环里，其他设备
	// 永远拿不到恢复证据。该设备要等另一台设备真正探测过之后才重新获得资格。
	lastFailedProbeDevice int64
	evidence              map[int64]*tdMissingDeviceEvidence
}

func (c *tdMissingCircuit) config() tdMissingCircuitSettings {
	return c.settings.withDefaults()
}

// allowQuery 在 TD 查询前决定是否放行，并在半开时占用唯一探测名额。
// 返回的 HalfOpenTransition 只表示本次把 open 推进到 half-open（指标在锁外记）。
// 半开探测只接受已过成熟线的样本：拿一条 TD 尚未写完的近期样本去探测，false
// 只能证明“还没写完”，不能证明系统性缺失仍在，会白白延长限流。
func (c *tdMissingCircuit) allowQuery(now time.Time, deviceID int64, receivedAt time.Time) tdMissingQueryDecision {
	c.mu.Lock()
	defer c.mu.Unlock()
	if !c.enabled {
		return tdMissingQueryDecision{Allow: true}
	}
	if c.state == tdMissingStateClosed {
		return tdMissingQueryDecision{Allow: true}
	}
	transitioned := false
	if c.state == tdMissingStateOpen {
		if now.Before(c.until) {
			return tdMissingQueryDecision{}
		}
		c.state = tdMissingStateHalfOpen
		c.probeInFlight = false
		c.evidence = nil
		transitioned = true
	}
	if c.state != tdMissingStateHalfOpen {
		return tdMissingQueryDecision{Allow: true, HalfOpenTransition: transitioned}
	}
	if c.probeInFlight {
		return tdMissingQueryDecision{HalfOpenTransition: transitioned}
	}
	if deviceID == c.lastFailedProbeDevice {
		return tdMissingQueryDecision{HalfOpenTransition: transitioned}
	}
	if receivedAt.IsZero() || now.Sub(receivedAt) < c.config().MatureAge {
		return tdMissingQueryDecision{HalfOpenTransition: transitioned}
	}
	c.probeInFlight = true
	return tdMissingQueryDecision{Allow: true, Probe: true, HalfOpenTransition: transitioned}
}

// abortProbe 释放未真正执行的半开探测（例如错误熔断先拒绝），不改变缺失状态。
func (c *tdMissingCircuit) abortProbe(probe bool) {
	if !probe {
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.state == tdMissingStateHalfOpen {
		c.probeInFlight = false
	}
}

// observeHit 记录一次真实命中。任何状态下的真实命中都证明 TD 对这条样本可查，
// 因此关闭缺失熔断；半开探测命中时 recovered=true，供调用方计数。
func (c *tdMissingCircuit) observeHit(now time.Time, deviceID int64) (recovered bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.evidence != nil {
		delete(c.evidence, deviceID)
	}
	if !c.enabled {
		return false
	}
	if c.state == tdMissingStateClosed {
		return false
	}
	c.state = tdMissingStateClosed
	c.until = time.Time{}
	c.probeInFlight = false
	c.lastFailedProbeDevice = 0
	c.evidence = nil
	return true
}

// observeMissing 记录一次 SQL 成功但无行。mature 只表示该样本已过接收成熟线；
// opened 表示本次观测把缺失熔断从 closed 推到 open（或半开探测失败重新开路）。
func (c *tdMissingCircuit) observeMissing(now time.Time, deviceID int64, receivedAt time.Time, probe bool) (mature, opened bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	cfg := c.config()
	mature = !receivedAt.IsZero() && now.Sub(receivedAt) >= cfg.MatureAge
	if !c.enabled {
		return mature, false
	}
	if c.state == tdMissingStateHalfOpen && probe {
		c.openLocked(now, cfg, deviceID)
		return mature, true
	}
	if c.state != tdMissingStateClosed {
		return mature, false
	}
	if !mature {
		return false, false
	}
	c.recordEvidenceLocked(now, deviceID, cfg)
	if c.qualifyingDevicesLocked(now, cfg) >= cfg.MinDevices {
		c.openLocked(now, cfg, 0)
		return true, true
	}
	return true, false
}

// observeError 只处理半开探测时的查询错误：错误本身归 tdCircuit，缺失熔断
// 不能把错误当成查无证据，但失败的探测必须释放名额并重新开路。
func (c *tdMissingCircuit) observeError(now time.Time, deviceID int64, probe bool) (opened bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if !c.enabled || !probe || c.state != tdMissingStateHalfOpen {
		return false
	}
	c.openLocked(now, c.config(), deviceID)
	return true
}

func (c *tdMissingCircuit) openLocked(now time.Time, cfg tdMissingCircuitSettings, failedDevice int64) {
	c.state = tdMissingStateOpen
	c.until = now.Add(cfg.Cooldown)
	c.probeInFlight = false
	c.evidence = nil
	if failedDevice > 0 {
		c.lastFailedProbeDevice = failedDevice
	} else {
		// 证据触发开路没有失败探测设备，旋转从零开始。
		c.lastFailedProbeDevice = 0
	}
}

func (c *tdMissingCircuit) recordEvidenceLocked(now time.Time, deviceID int64, cfg tdMissingCircuitSettings) {
	if c.evidence == nil {
		c.evidence = make(map[int64]*tdMissingDeviceEvidence)
	}
	// 先清掉窗口外的旧证据，保证“持续”不是历史累计。
	for id, ev := range c.evidence {
		if now.Sub(ev.LastSeen) > cfg.EvidenceWindow {
			delete(c.evidence, id)
		}
	}
	ev := c.evidence[deviceID]
	// 只有 LastSeen 新鲜还不够：FirstSeen 是连续证据的起点，它一旦滑出窗口，
	// 之前累计的 Checks 和跨度都属于更早的窗口，必须从本次观测重新起算。
	// 否则每 2 分钟一次的偶发查无会凭数小时的 Checks 被拼成“持续”。
	// 边界与清理/判定一致：恰好等于窗口仍算窗内，严格超过才重置。
	if ev != nil && now.Sub(ev.FirstSeen) > cfg.EvidenceWindow {
		ev.FirstSeen = now
		ev.Checks = 0
	}
	if ev == nil {
		ev = &tdMissingDeviceEvidence{FirstSeen: now}
		c.evidence[deviceID] = ev
	}
	ev.LastSeen = now
	ev.Checks++
	// 固定上限：超过时淘汰最久未见的设备，绝不按设备无限扩容。
	if len(c.evidence) > cfg.EvidenceLimit {
		var oldestID int64
		var oldest time.Time
		for id, item := range c.evidence {
			if oldest.IsZero() || item.LastSeen.Before(oldest) {
				oldestID, oldest = id, item.LastSeen
			}
		}
		delete(c.evidence, oldestID)
	}
}

func (c *tdMissingCircuit) qualifyingDevicesLocked(now time.Time, cfg tdMissingCircuitSettings) int {
	n := 0
	for _, ev := range c.evidence {
		// 窗口以 FirstSeen 锚定；即使清理顺序或运行时设置变化留下陈旧条目，
		// 也不能把窗口外的观测计为当前持续证据。
		if now.Sub(ev.FirstSeen) > cfg.EvidenceWindow {
			continue
		}
		if now.Sub(ev.LastSeen) > cfg.EvidenceWindow {
			continue
		}
		if ev.Checks < cfg.MinDeviceChecks {
			continue
		}
		if ev.LastSeen.Sub(ev.FirstSeen) < cfg.ConfirmSpan {
			continue
		}
		n++
	}
	return n
}

// snapshot 返回 0=closed、1=open、2=half-open。冷却已过但尚未真正探测时也显示
// 2，表示“已具备半开资格”；它不修改内部状态，探测名额仍由 allowQuery 原子占用。
func (c *tdMissingCircuit) snapshot(now time.Time) float64 {
	c.mu.Lock()
	defer c.mu.Unlock()
	if !c.enabled {
		return float64(tdMissingStateClosed)
	}
	switch c.state {
	case tdMissingStateOpen:
		if !now.Before(c.until) {
			return float64(tdMissingStateHalfOpen)
		}
		return float64(tdMissingStateOpen)
	case tdMissingStateHalfOpen:
		return float64(tdMissingStateHalfOpen)
	default:
		return float64(tdMissingStateClosed)
	}
}
