package api

import (
	"context"
	"errors"
	"math"
	"sort"
	"strconv"
	"sync"
	"time"

	"github.com/gin-gonic/gin"

	"powersystem/backend/internal/model"
)

const (
	reportMaxDays    = 31 * 24 * time.Hour
	availabilityRule = "availability-v1"
	maxIntervalMs    = 15_000
)

// availabilityResult 是单设备可用率结果。
type availabilityResult struct {
	DeviceID      int64    `json:"device_id"`
	DeviceCode    string   `json:"device_code"`
	StationCode   string   `json:"station_code"`
	CoveredMs     int64    `json:"covered_ms"`
	WindowMs      int64    `json:"window_ms"`
	RunningMs     int64    `json:"running_ms"`
	StoppedMs     int64    `json:"stopped_ms"`
	FaultMs       int64    `json:"fault_ms"`
	UnknownMs     int64    `json:"unknown_ms"`
	ObservedAvail *float64 `json:"observed_availability"`
	DataCoverage  *float64 `json:"data_coverage"`
	Insufficient  bool     `json:"insufficient_data"`
	RuleVersion   string   `json:"rule_version"`
}

// samplePoint is a minimal status sample with a timestamp.
type samplePoint struct {
	TS     int64
	Status int
}

// computeAvailability builds status intervals from raw samples. Each sample
// extends forward to the next sample, capped at 15s; the excess is unknown.
func computeAvailability(samples []samplePoint, start, end int64) availabilityResult {
	sort.SliceStable(samples, func(i, j int) bool { return samples[i].TS < samples[j].TS })
	acc := availabilityAccumulator{start: start, end: end}
	for _, point := range samples {
		acc.add(point)
	}
	return acc.finish()
}

// Carry one pending sample across pages; intervals crossing a page boundary
// must end at the next sample, rather than at the end of the page.
type availabilityAccumulator struct {
	start, end int64
	last       samplePoint
	hasLast    bool
	result     availabilityResult
}

func (a *availabilityAccumulator) add(point samplePoint) {
	if a.hasLast {
		a.close(point.TS)
	}
	a.last = point
	a.hasLast = true
}

func (a *availabilityAccumulator) close(next int64) {
	left := max(a.last.TS, a.start)
	right := min3(a.last.TS+maxIntervalMs, next, a.end)
	if right <= left {
		return
	}
	duration := right - left
	a.result.CoveredMs += duration
	switch a.last.Status {
	case 1:
		a.result.RunningMs += duration
	case 0:
		a.result.StoppedMs += duration
	case 2:
		a.result.FaultMs += duration
	default:
		a.result.UnknownMs += duration
	}
}

func (a *availabilityAccumulator) finish() availabilityResult {
	if a.hasLast {
		a.close(a.end)
	}
	res := a.result
	res.WindowMs = a.end - a.start
	res.RuleVersion = availabilityRule
	res.UnknownMs += max(0, res.WindowMs-res.CoveredMs)
	if res.WindowMs <= 0 {
		res.Insufficient = true
		return res
	}
	coverage := float64(res.CoveredMs) / float64(res.WindowMs)
	res.DataCoverage = &coverage
	res.Insufficient = coverage < 0.90
	if res.CoveredMs > 0 {
		avail := float64(res.RunningMs+res.StoppedMs) / float64(res.CoveredMs)
		res.ObservedAvail = &avail
	} else {
		res.Insufficient = true
	}
	return res
}

// loadAvailability folds ordered status pages in constant memory. The boundary
// sample supplies coverage at window start; the window end remains exclusive.
func (s *Server) loadAvailability(ctx context.Context, device model.Device, start, end int64) (availabilityResult, error) {
	acc := availabilityAccumulator{start: start, end: end}
	if err := s.scanAvailability(ctx, device, start, end, acc.add); err != nil {
		return availabilityResult{}, err
	}
	return acc.finish(), nil
}

func (s *Server) scanAvailability(ctx context.Context, device model.Device, start, end int64, visit func(samplePoint)) error {
	if err := s.td.EnsureDevice(ctx, device); err != nil {
		return err
	}
	boundary, err := s.td.BoundarySample(ctx, device.ID, start)
	if err != nil {
		return err
	}
	if len(boundary) > 0 {
		point, err := parseAvailabilitySample(boundary[0], 1)
		if err != nil || point.TS > start {
			return errors.New("invalid boundary sample")
		}
		visit(point)
	}
	cursor := start - 1
	for {
		rows, err := s.td.StatusAfter(ctx, device.ID, cursor, end, exportPageSize)
		if err != nil {
			return err
		}
		for _, row := range rows {
			point, err := parseAvailabilitySample(row, 1)
			if err != nil || point.TS <= cursor || point.TS >= end {
				return errors.New("invalid or unordered availability sample")
			}
			visit(point)
			cursor = point.TS
		}
		if len(rows) < exportPageSize {
			break
		}
	}
	return nil
}

// Bound per-request database work while preserving ledger order. Any device
// failure cancels sibling reads and prevents a partial report or CSV download.
func (s *Server) availabilityDevices(ctx context.Context, devices []model.Device, start, end time.Time) ([]availabilityResult, error) {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	results := make([]availabilityResult, len(devices))
	jobs := make(chan int, len(devices))
	for i := range devices {
		jobs <- i
	}
	close(jobs)
	var wg sync.WaitGroup
	var once sync.Once
	var firstErr error
	for worker := 0; worker < min(3, len(devices)); worker++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := range jobs {
				if ctx.Err() != nil {
					return
				}
				d := devices[i]
				deviceStart := start
				if d.CreatedAt.After(deviceStart) {
					deviceStart = d.CreatedAt
				}
				res, err := s.loadAvailability(ctx, d, deviceStart.UnixMilli(), end.UnixMilli())
				if err != nil {
					once.Do(func() { firstErr = err; cancel() })
					return
				}
				res.DeviceID, res.DeviceCode, res.StationCode = d.ID, d.DeviceCode, d.StationCode
				results[i] = res
			}
		}()
	}
	wg.Wait()
	if firstErr != nil {
		return nil, firstErr
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	return results, nil
}

// A malformed row is a read failure, not evidence of a telemetry gap.
func parseAvailabilitySample(row []any, statusIndex int) (samplePoint, error) {
	if len(row) <= statusIndex {
		return samplePoint{}, errors.New("invalid availability sample")
	}
	ts, err := parseTDTime(row[0])
	status, valid := asInt64(row, statusIndex)
	if err != nil || ts <= 0 || !valid || status < 0 || status > 2 {
		return samplePoint{}, errors.New("invalid availability sample")
	}
	return samplePoint{TS: ts, Status: int(status)}, nil
}

func (s *Server) availability(c *gin.Context) {
	start, end, valid := s.reportWindow(c)
	if !valid {
		return
	}
	deviceID := int64(0)
	if v := c.Query("device_id"); v != "" {
		var err error
		deviceID, err = parseInt64(v)
		if err != nil || deviceID < 1 {
			fail(c, 400, 40001, "invalid device_id")
			return
		}
	}
	station := c.Query("station_code")
	devices, err := s.findDevices()
	if err != nil {
		fail(c, 500, 50000, "availability failed")
		return
	}
	selected := make([]model.Device, 0, len(devices))
	for _, d := range devices {
		if deviceID > 0 && d.ID != deviceID {
			continue
		}
		if station != "" && d.StationCode != station {
			continue
		}
		if !d.CreatedAt.Before(end) {
			continue
		}
		selected = append(selected, d)
	}
	results, metadata, err := s.availabilityReportDevices(c.Request.Context(), selected, start, end)
	if err != nil {
		fail(c, 503, 50000, "telemetry unavailable")
		return
	}
	stationAgg := s.aggregateAvailability(results)
	computedAt := time.Now().UTC().Format(time.RFC3339Nano)
	c.Header("X-Statistics-Updated-At", metadata.UpdatedAt)
	c.Header("X-Aggregation-State", metadata.State)
	c.Header("X-Aggregation-Lag-Ms", strconv.FormatInt(metadata.LagMs, 10))
	if c.Query("format") == "csv" {
		s.availabilityCSV(c, results, stationAgg, start, end, computedAt)
		return
	}
	ok(c, gin.H{"rule_version": availabilityRule, "computed_at": computedAt,
		"statistics_updated_at": metadata.UpdatedAt, "aggregation_state": metadata.State, "aggregation_lag_ms": metadata.LagMs,
		"start": start, "end": end, "devices": results, "stations": stationAgg,
		"complete": true, "grouping": "current_ledger", "basis": "current ledger, not historical station assignment; window starts at device creation; samples extend up to 15s; coverage<90% insufficient"})
}

// stationAvailability aggregates durations then computes ratios, never averaging device percentages.
type stationAvailability struct {
	StationCode   string   `json:"station_code"`
	CoveredMs     int64    `json:"covered_ms"`
	WindowMs      int64    `json:"window_ms"`
	NonFaultMs    int64    `json:"non_fault_ms"`
	RunningMs     int64    `json:"running_ms"`
	StoppedMs     int64    `json:"stopped_ms"`
	FaultMs       int64    `json:"fault_ms"`
	UnknownMs     int64    `json:"unknown_ms"`
	ObservedAvail *float64 `json:"observed_availability"`
	DataCoverage  *float64 `json:"data_coverage"`
	Insufficient  bool     `json:"insufficient_data"`
	DeviceCount   int      `json:"device_count"`
}

func (s *Server) aggregateAvailability(results []availabilityResult) []stationAvailability {
	byStation := make(map[string]*stationAvailability)
	for _, r := range results {
		st, ok := byStation[r.StationCode]
		if !ok {
			st = &stationAvailability{StationCode: r.StationCode}
			byStation[r.StationCode] = st
		}
		st.CoveredMs += r.CoveredMs
		st.WindowMs += r.WindowMs
		st.NonFaultMs += r.RunningMs + r.StoppedMs
		st.RunningMs += r.RunningMs
		st.StoppedMs += r.StoppedMs
		st.FaultMs += r.FaultMs
		st.UnknownMs += r.UnknownMs
		st.DeviceCount++
	}
	out := make([]stationAvailability, 0, len(byStation))
	for _, st := range byStation {
		if st.CoveredMs > 0 {
			avail := float64(st.NonFaultMs) / float64(st.CoveredMs)
			st.ObservedAvail = &avail
		}
		if st.WindowMs > 0 {
			coverage := float64(st.CoveredMs) / float64(st.WindowMs)
			st.DataCoverage = &coverage
			if coverage < 0.90 {
				st.Insufficient = true
			}
		} else {
			st.Insufficient = true
		}
		if st.ObservedAvail == nil {
			st.Insufficient = true
		}
		out = append(out, *st)
	}
	sort.SliceStable(out, func(i, j int) bool { return out[i].StationCode < out[j].StationCode })
	return out
}

// handleTimeliness 处理时效：触发至确认、触发至平台告警恢复，分别统计有效样本数、均值、中位数、P90。
type timelinessStat struct {
	Count    int      `json:"count"`
	MeanMs   *float64 `json:"mean_ms"`
	MedianMs *float64 `json:"median_ms"`
	P90Ms    *float64 `json:"p90_ms"`
}

type timelinessResult struct {
	Scope                       string         `json:"scope"`
	AckTimeliness               timelinessStat `json:"ack_timeliness"`
	RecoverTimeliness           timelinessStat `json:"recover_timeliness"`
	PendingAck                  int            `json:"pending_ack"`
	PendingRecover              int            `json:"pending_recover"`
	ManagedOrUnknownRecover     int            `json:"managed_or_unknown_recover"`
	TelemetryRecoverTimeliness  timelinessStat `json:"telemetry_recover_timeliness"`
	PredictionRecoverTimeliness timelinessStat `json:"prediction_recover_timeliness"`
	ManagedRecover              int            `json:"managed_recover"`
	UnknownRecover              int            `json:"unknown_recover"`
	RecoveryReasons             map[string]int `json:"recovery_reasons"`
	EventCount                  int            `json:"event_count"`
	InvalidLifecycle            int            `json:"invalid_lifecycle"`
}

func computeTimeliness(durations []float64) timelinessStat {
	stat := timelinessStat{Count: len(durations)}
	if len(durations) == 0 {
		return stat
	}
	sort.Float64s(durations)
	mean := 0.0
	for _, d := range durations {
		mean += d
	}
	mean /= float64(len(durations))
	stat.MeanMs = &mean
	median := durations[len(durations)/2]
	if len(durations)%2 == 0 {
		median = (durations[len(durations)/2-1] + durations[len(durations)/2]) / 2
	}
	stat.MedianMs = &median
	p90 := durations[int(math.Ceil(float64(len(durations))*0.9))-1]
	stat.P90Ms = &p90
	return stat
}

func (s *Server) alarmTimeliness(c *gin.Context) {
	start, end, valid := s.reportWindow(c)
	if !valid {
		return
	}
	deviceID := int64(0)
	if v := c.Query("device_id"); v != "" {
		var err error
		deviceID, err = parseInt64(v)
		if err != nil || deviceID < 1 {
			fail(c, 400, 40001, "invalid device_id")
			return
		}
	}
	station := c.Query("station_code")
	query := s.db.Model(&model.AlarmRecord{}).Where("triggered_at >= ? AND triggered_at < ?", start, end)
	if deviceID > 0 {
		query = query.Where("device_id = ?", deviceID)
	}
	if station != "" {
		query = query.Where("device_id IN (?)", s.db.Model(&model.Device{}).Select("id").Where("station_code = ? AND deleted_at IS NULL", station))
	}
	var alarms []model.AlarmRecord
	if err := query.Find(&alarms).Error; err != nil {
		fail(c, 500, 50000, "timeliness query failed")
		return
	}
	res := summarizeTimeliness(alarms, end)
	res.Scope = scopeLabel(deviceID, station)
	computedAt := time.Now().UTC().Format(time.RFC3339Nano)
	if c.Query("format") == "csv" {
		s.timelinessCSV(c, res, start, end, computedAt)
		return
	}
	ok(c, gin.H{"rule_version": "timeliness-v1", "computed_at": computedAt, "start": start, "end": end, "scope": res.Scope, "result": res, "complete": res.InvalidLifecycle == 0, "grouping": "current_ledger", "basis": "events triggered in [start,end); platform recorded lifecycle as of end; telemetry and prediction recovery separated; not repair duration or MTTR"})
}

func summarizeTimeliness(alarms []model.AlarmRecord, end time.Time) timelinessResult {
	res := timelinessResult{EventCount: len(alarms), RecoveryReasons: map[string]int{}}
	var ackDurations, recoverDurations, telemetryDurations, predictionDurations []float64
	for _, a := range alarms {
		if a.AckedAt != nil && a.AckedAt.Before(a.TriggeredAt) {
			res.InvalidLifecycle++
			res.PendingAck++
		} else if a.AckedAt != nil && !a.AckedAt.After(end) {
			ackDurations = append(ackDurations, float64(a.AckedAt.Sub(a.TriggeredAt).Milliseconds()))
		} else {
			// Confirmation is independent of recovery, and observed only up to end.
			res.PendingAck++
		}
		if a.RecoveredAt != nil && a.RecoveredAt.Before(a.TriggeredAt) {
			res.InvalidLifecycle++
			res.PendingRecover++
			continue
		}
		if a.RecoveredAt != nil && !a.RecoveredAt.After(end) {
			reason := "unknown"
			if a.CloseReason != nil && *a.CloseReason != "" {
				reason = *a.CloseReason
			}
			res.RecoveryReasons[reason]++
			if a.CloseReason != nil && (*a.CloseReason == "telemetry_recovered" || *a.CloseReason == "prediction_risk_resolved") {
				duration := float64(a.RecoveredAt.Sub(a.TriggeredAt).Milliseconds())
				recoverDurations = append(recoverDurations, duration)
				if reason == "telemetry_recovered" {
					telemetryDurations = append(telemetryDurations, duration)
				} else {
					predictionDurations = append(predictionDurations, duration)
				}
			} else {
				res.ManagedOrUnknownRecover++
				if reason == "rule_disabled" || reason == "rule_deleted" || reason == "device_deleted" {
					res.ManagedRecover++
				} else {
					res.UnknownRecover++
				}
			}
		} else {
			res.PendingRecover++
		}
	}
	res.AckTimeliness = computeTimeliness(ackDurations)
	res.RecoverTimeliness = computeTimeliness(recoverDurations)
	res.TelemetryRecoverTimeliness = computeTimeliness(telemetryDurations)
	res.PredictionRecoverTimeliness = computeTimeliness(predictionDurations)
	return res
}

func (s *Server) reportWindow(c *gin.Context) (time.Time, time.Time, bool) {
	if format := c.Query("format"); format != "" && format != "csv" {
		fail(c, 400, 40001, "format must be csv or omitted")
		return time.Time{}, time.Time{}, false
	}
	now := time.Now().UTC()
	end := now
	if v := c.Query("end"); v != "" {
		parsed, err := time.Parse(time.RFC3339, v)
		if err != nil {
			fail(c, 400, 40001, "end requires RFC3339")
			return time.Time{}, time.Time{}, false
		}
		end = parsed
	}
	start := end.AddDate(0, 0, -7)
	if v := c.Query("start"); v != "" {
		parsed, err := time.Parse(time.RFC3339, v)
		if err != nil {
			fail(c, 400, 40001, "start requires RFC3339")
			return time.Time{}, time.Time{}, false
		}
		start = parsed
	}
	if !end.After(start) || end.Sub(start) > reportMaxDays || end.After(now.Add(time.Minute)) {
		fail(c, 400, 40001, "invalid report window")
		return time.Time{}, time.Time{}, false
	}
	return start.UTC(), end.UTC(), true
}

func scopeLabel(deviceID int64, station string) string {
	if deviceID > 0 {
		return "device:" + itoa(deviceID)
	}
	if station != "" {
		return "station:" + station
	}
	return "all"
}

func parseInt64(v string) (int64, error) {
	return strconv.ParseInt(v, 10, 64)
}

func itoa(v int64) string { return strconv.FormatInt(v, 10) }

func min3(a, b, c int64) int64 { return min(a, min(b, c)) }
