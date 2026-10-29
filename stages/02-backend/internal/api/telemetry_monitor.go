package api

import (
	"context"
	"encoding/json"
	"fmt"
	"math"
	"strings"
	"sync"
	"time"

	"github.com/gin-gonic/gin"
	"go.uber.org/zap"
	"powersystem/backend/internal/model"
	"powersystem/backend/internal/service"
	"powersystem/backend/internal/telemetry"
)

const telemetryProbeInterval = 5 * time.Second
const telemetryProbeBudget = 4 * time.Second
const telemetryFreshWindow = 15 * time.Second

type telemetryMonitor struct {
	durationCount map[string]uint64
	durationSum   map[string]time.Duration
	durationMax   map[string]time.Duration

	mu       sync.Mutex
	counts   map[string]uint64
	values   map[string]float64
	times    map[string]time.Time
	failures map[string]uint64
	logs     map[string]time.Time
}

func (m *telemetryMonitor) touch(key string, at time.Time) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.times == nil {
		m.times = map[string]time.Time{}
	}
	m.times[key] = at
	if m.counts == nil {
		m.counts = map[string]uint64{}
	}
	m.counts[key]++
}
func (m *telemetryMonitor) failure(code string, quarantine bool, at time.Time) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.failures == nil {
		m.failures = map[string]uint64{}
	}
	m.failures[code]++
	if quarantine {
		if m.times == nil {
			m.times = map[string]time.Time{}
		}
		m.times["advance"] = at
		if m.counts == nil {
			m.counts = map[string]uint64{}
		}
		m.counts["advance"]++
	}
}
func (s *Server) telemetryLog(code string, now time.Time) {
	m := &s.telemetry
	m.mu.Lock()
	if m.logs == nil {
		m.logs = map[string]time.Time{}
	}
	last := m.logs[code]
	allowed := last.IsZero() || now.Sub(last) >= time.Minute
	if allowed {
		m.logs[code] = now
	}
	m.mu.Unlock()
	if allowed && s.log != nil {
		s.log.Warn("telemetry_state", zap.String("reason", code))
	}
}
func (s *Server) telemetryWatchdog(ctx context.Context) {
	ticker := time.NewTicker(telemetryProbeInterval)
	defer ticker.Stop()
	for {
		s.probeTelemetry(ctx)
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}

// Registered devices are authoritative. Silence windows are explicit, bounded,
// actor/reason audited rows. A missing fleet only makes classification unknown.
type expectedTelemetryDevice struct {
	model.Device
	Silent bool
	Scope  string
}

func (s *Server) expectedTelemetryDevices(ctx context.Context, now time.Time) ([]expectedTelemetryDevice, bool, error) {
	var devices []expectedTelemetryDevice
	err := s.db.WithContext(ctx).Raw(`SELECT d.*, EXISTS(SELECT 1 FROM telemetry_silence_windows w WHERE w.device_id=d.id AND w.starts_at<=? AND w.ends_at>?) AS silent FROM devices d WHERE d.deleted_at IS NULL ORDER BY d.id`, now, now).Scan(&devices).Error
	identities, identityErr := service.SimulationCatalogIdentities(s.fleetCatalog)
	known := identityErr == nil
	identityStations := map[string]string{}
	for _, identity := range identities {
		identityStations[identity.DeviceCode] = identity.StationCode
	}
	for i := range devices {
		d := &devices[i]
		d.Scope = "unknown"
		station, listed := identityStations[d.DeviceCode]
		if listed {
			if station != d.StationCode || !d.IsSimulated {
				known = false
				continue
			}
			d.Scope, _ = s.fleetCatalog.ScopeOf(station)
		} else if d.IsSimulated {
			known = false
		}
	}
	return devices, known, err
}
func unknownTelemetryValues() map[string]float64 {
	v := map[string]float64{}
	for _, key := range []string{"inbox_receive_age_seconds", "business_success_age_seconds", "queue_oldest_age_seconds", "expected_devices", "fresh_devices", "fresh_ratio", "missing_keys", "cache_received_worst_age_seconds", "cache_sample_worst_age_seconds", "maintenance", "maintenance_age_seconds", "classification_known", "maintenance_missing", "maintenance_sql_error"} {
		v[key] = math.NaN()
	}
	v["maintenance_known"] = 0
	v["probe_pg_ok"] = 0
	v["probe_redis_ok"] = 0
	v["probe_td_ok"] = 0
	v["probe_td_state"] = 0
	return v
}
func (s *Server) probeTelemetry(parent context.Context) {
	now := time.Now()
	v := unknownTelemetryValues()
	v["probe_measured_timestamp_seconds"] = telemetryTimestamp(now)
	ctx, cancel := context.WithTimeout(parent, telemetryProbeBudget)
	defer cancel()
	// These indexed top-one queries never COUNT or MAX the inbox history.
	var row struct {
		ReceivedAt *time.Time
		SuccessAt  *time.Time
		QueueAt    *time.Time
	}
	err := s.db.WithContext(ctx).Raw(`SELECT
 (SELECT received_at FROM telemetry_inbox ORDER BY received_at DESC LIMIT 1) AS received_at,
 (SELECT processed_at FROM telemetry_inbox WHERE business_state='success' ORDER BY processed_at DESC LIMIT 1) AS success_at,
 (SELECT received_at FROM telemetry_inbox WHERE processed_at IS NULL ORDER BY id LIMIT 1) AS queue_at`).Scan(&row).Error
	if err == nil {
		v["inbox_receive_age_seconds"] = telemetryAge(row.ReceivedAt, now)
		v["business_success_age_seconds"] = telemetryAge(row.SuccessAt, now)
		if row.QueueAt == nil {
			v["queue_oldest_age_seconds"] = 0
		} else {
			v["queue_oldest_age_seconds"] = telemetryAge(row.QueueAt, now)
		}
	}
	var maint struct {
		Enabled   bool
		UpdatedAt time.Time
	}
	maintenance := s.db.WithContext(ctx).Raw("SELECT enabled,updated_at FROM alarm_maintenance_state WHERE id=1").Scan(&maint)
	v["maintenance_sql_error"] = 0
	v["maintenance_missing"] = 0
	if maintenance.Error != nil {
		v["maintenance_sql_error"] = 1
	} else if maintenance.RowsAffected != 1 {
		v["maintenance_missing"] = 1
	}
	if maintenance.Error == nil && maintenance.RowsAffected == 1 {
		v["maintenance_known"] = 1
		v["maintenance"] = 0
		if maint.Enabled {
			v["maintenance"] = 1
		}
		v["maintenance_age_seconds"] = telemetryAge(&maint.UpdatedAt, now)
	}
	devices, known, deviceErr := s.expectedTelemetryDevices(ctx, now)
	// Readiness includes TD availability, even when restore candidates already
	// carry a durable TD confirmation. This is a bounded read, not a history scan.
	if s.td != nil && ctx.Err() == nil {
		s.telemetry.touch("td_probe_attempt", time.Now())
		dep, finish := context.WithTimeout(ctx, telemetryDependencyBudget)
		if _, err := s.td.SQL(dep, "SELECT 1"); err == nil {
			v["probe_td_ok"] = 1
			v["probe_td_state"] = 1
			s.telemetry.touch("td_probe_success", time.Now())
		} else if dep.Err() != nil {
			v["probe_td_state"] = 3
			s.telemetry.touch("td_probe_timeout", time.Now())
			s.telemetryLog("probe_td_timeout", now)
		} else {
			v["probe_td_state"] = 2
			s.telemetry.touch("td_probe_error", time.Now())
			s.telemetryLog("probe_td_error", now)
		}
		finish()
	} else {
		v["probe_td_state"] = 4
		s.telemetry.touch("td_probe_skipped", time.Now())
		s.telemetryLog("probe_td_skipped", now)
	}
	if err == nil && deviceErr == nil && maintenance.Error == nil {
		v["probe_pg_ok"] = 1
	}
	if deviceErr == nil {
		expected := 0
		for _, d := range devices {
			if !d.Silent {
				expected++
			}
		}
		v["expected_devices"] = float64(expected)
		v["classification_known"] = 0
		if known {
			v["classification_known"] = 1
		}
		keys := []string{}
		for _, d := range devices {
			if !d.Silent {
				keys = append(keys, fmt.Sprintf("device:%d:latest", d.ID))
			}
		}
		if s.redis != nil {
			// PING even an intentionally empty registry so Redis errors stay visible.
			redisErr := s.redis.Ping(ctx).Err()
			if redisErr == nil {
				values := []any{}
				for start := 0; start < len(keys); start += 256 {
					end := min(start+256, len(keys))
					batch, e := s.redis.MGet(ctx, keys[start:end]...).Result()
					if e != nil {
						redisErr = e
						break
					}
					values = append(values, batch...)
				}
				if redisErr == nil {
					v["probe_redis_ok"] = 1
					fresh, missing := 0, 0
					worstRecv, worstSample := 0.0, 0.0
					for _, raw := range values {
						text, ok := raw.(string)
						if !ok {
							missing++
							continue
						}
						var sample struct {
							ReceivedAt *time.Time `json:"received_at"`
							TS         *int64     `json:"ts_ms"`
						}
						if json.Unmarshal([]byte(text), &sample) != nil || sample.ReceivedAt == nil || sample.TS == nil {
							missing++
							continue
						}
						sampled := time.UnixMilli(*sample.TS)
						receivedAge := telemetryAge(sample.ReceivedAt, now)
						sampleAge := math.Max(0, now.Sub(sampled).Seconds())
						if *sample.TS <= 0 || sampled.After(now.Add(60*time.Second)) || sampled.After(sample.ReceivedAt.Add(60*time.Second)) {
							sampleAge = math.NaN()
						}
						if math.IsNaN(receivedAge) || math.IsNaN(sampleAge) {
							missing++
							continue
						}
						worstRecv = math.Max(worstRecv, receivedAge)
						worstSample = math.Max(worstSample, sampleAge)
						if telemetry.Freshness(now, *sample.ReceivedAt, *sample.TS) == "fresh" {
							fresh++
						}
					}
					v["fresh_devices"] = float64(fresh)
					v["missing_keys"] = float64(missing)
					if expected > 0 {
						v["fresh_ratio"] = float64(fresh) / float64(expected)
					}
					if len(values) > missing {
						v["cache_received_worst_age_seconds"] = worstRecv
						v["cache_sample_worst_age_seconds"] = worstSample
					}
				}
			}
		}
	}
	s.telemetry.mu.Lock()
	s.telemetry.values = v
	if s.telemetry.times == nil {
		s.telemetry.times = map[string]time.Time{}
	}
	s.telemetry.times["probe"] = time.Now()
	s.telemetry.mu.Unlock()
	if v["probe_pg_ok"] == 0 {
		s.telemetryLog("probe_pg", now)
	}
	if v["probe_redis_ok"] == 0 {
		s.telemetryLog("probe_redis", now)
	}
}
func telemetryAge(at *time.Time, now time.Time) float64 {
	if at == nil || at.IsZero() || at.After(now) {
		return math.NaN()
	}
	return now.Sub(*at).Seconds()
}
func (s *Server) serveTelemetryMetrics(c *gin.Context) {
	// The handler performs no database/cache I/O. Ages keep increasing if a loop stops.
	m := &s.telemetry
	m.mu.Lock()
	var b strings.Builder
	values := m.values
	if values == nil {
		values = unknownTelemetryValues()
	}
	for key, value := range values {
		fmt.Fprintf(&b, "powersystem_telemetry_%s %g\n", key, value)
	}
	fmt.Fprintf(&b, "powersystem_telemetry_admission_inflight %d\n", len(s.ingressSlots))
	fmt.Fprintf(&b, "powersystem_telemetry_admission_queued %d\n", len(s.ingressQueue))
	fmt.Fprintf(&b, "powersystem_telemetry_admission_waiting_ack %d\n", len(s.ingressAcks))
	headAge := float64(0)
	if head := s.ingressHead.Load(); head > 0 {
		headAge = time.Since(time.Unix(0, head)).Seconds()
	}
	fmt.Fprintf(&b, "powersystem_telemetry_admission_head_age_seconds %g\n", headAge)
	for _, key := range []string{"ingress_ack_requested", "ingress_oversize"} {
		fmt.Fprintf(&b, "powersystem_telemetry_%s_total %d\n", key, m.counts[key])
	}
	for _, key := range []string{"start", "probe", "loop", "advance", "success", "redis_write", "cancelled", "commit_unknown", "gate_disabled", "gate_enabled", "gate_sql_error", "gate_missing", "ws_drop", "ws_slow", "ws_write_timeout", "td_probe_attempt", "td_probe_success", "td_probe_timeout", "td_probe_error", "td_probe_skipped", "scheduler_visit", "scheduler_wrap", "scheduler_budget", "retry", "quarantine", "td_circuit_attempt", "td_circuit_skip", "redis_circuit_attempt", "redis_circuit_skip"} {
		at := m.times[key]
		fmt.Fprintf(&b, "powersystem_telemetry_%s_total %d\n", key, m.counts[key])
		fmt.Fprintf(&b, "powersystem_telemetry_%s_age_seconds %g\n", key, telemetryAge(&at, time.Now()))
		fmt.Fprintf(&b, "powersystem_telemetry_%s_timestamp_seconds %g\n", key, telemetryTimestamp(at))
	}
	for _, path := range []string{"history", "business_pg", "snapshot", "ingress", "cursor", "td_confirm"} {
		fmt.Fprintf(&b, "powersystem_telemetry_path_seconds_sum{path=%q} %g\n", path, m.durationSum[path].Seconds())
		fmt.Fprintf(&b, "powersystem_telemetry_path_seconds_count{path=%q} %d\n", path, m.durationCount[path])
	}
	for _, code := range []string{"payload_invalid", "identity_invalid", "device_deleted", "td_unavailable", "td_missing", "device_lookup", "redis_unavailable", "business_failed"} {
		fmt.Fprintf(&b, "powersystem_telemetry_failures_total{reason=%q} %d\n", code, m.failures[code])
	}
	m.mu.Unlock()
	// Existing HTTP metrics share this response, without exposing dynamic IDs.
	httpMetrics.serve(c)
	// gin Data writes directly, so append telemetry after the existing body.
	c.Writer.Write([]byte(b.String()))
}

func telemetryTimestamp(at time.Time) float64 {
	if at.IsZero() {
		return 0
	}
	return float64(at.UnixNano()) / 1e9
}

func (s *Server) setStartup(phase string, restored bool) {
	s.startupMu.Lock()
	s.startupPhase = phase
	s.restoreOK = restored
	s.startupMu.Unlock()
	if s.log != nil {
		s.log.Info("telemetry_startup", zap.String("phase", phase))
	}
}
func (s *Server) readinessSnapshot() (bool, string, bool) {
	s.startupMu.Lock()
	phase, restored := s.startupPhase, s.restoreOK
	s.startupMu.Unlock()
	s.telemetry.mu.Lock()
	v := s.telemetry.values
	ready := restored && phase == "running" && v["probe_pg_ok"] == 1 && v["probe_redis_ok"] == 1 && v["probe_td_ok"] == 1 && v["maintenance_known"] == 1 && v["maintenance"] == 0 && v["classification_known"] == 1 && v["expected_devices"] > 0 && v["fresh_ratio"] == 1
	at := s.telemetry.times["probe"]
	elapsed := time.Since(at)
	measuredAge := telemetryTimestamp(time.Now()) - v["probe_measured_timestamp_seconds"]
	dataElapsed := math.Max(elapsed.Seconds(), measuredAge)
	ready = ready && !at.IsZero() && elapsed >= 0 && measuredAge >= 0 && dataElapsed < 15 && v["cache_received_worst_age_seconds"]+dataElapsed <= 15 && v["cache_sample_worst_age_seconds"]+dataElapsed <= 15
	s.telemetry.mu.Unlock()
	return ready, phase, restored
}
func (s *Server) telemetryReady(c *gin.Context) {
	ready, phase, restored := s.readinessSnapshot()
	status := 503
	if ready {
		status = 200
	}
	c.JSON(status, gin.H{"ready": ready, "phase": phase, "restore_complete": restored})
}

// All callers use fixed path names; no device/error strings are labels.
func (m *telemetryMonitor) elapsed(path string, d time.Duration) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.durationCount == nil {
		m.durationCount = map[string]uint64{}
		m.durationSum = map[string]time.Duration{}
		m.durationMax = map[string]time.Duration{}
	}
	m.durationCount[path]++
	m.durationSum[path] += d
	if d > m.durationMax[path] {
		m.durationMax[path] = d
	}
}
func (m *telemetryMonitor) durations() map[string]any {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := map[string]any{}
	for path, n := range m.durationCount {
		out[path] = map[string]any{"count": n, "mean_ms": float64(m.durationSum[path].Microseconds()) / float64(n) / 1000, "max_ms": float64(m.durationMax[path].Microseconds()) / 1000}
	}
	return out
}
