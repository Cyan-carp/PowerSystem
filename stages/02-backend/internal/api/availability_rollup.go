package api

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"sync"
	"time"

	"gorm.io/gorm"
	"powersystem/backend/internal/model"
)

const availabilityHour = int64(3_600_000)
const availabilityRollupLock = int64(726019017)

type rollupState struct {
	WorkerEnabled bool
	ReadEnabled   bool
	SeededHourMs  int64
	SweptAt       *time.Time
}
type hourlyAvailability struct {
	DeviceID           int64
	HourMs             int64
	RuleVersion        string
	Generation         int64
	ComputedGeneration int64
	CoveredMs          int64
	RunningMs          int64
	StoppedMs          int64
	FaultMs            int64
	UnknownMs          int64
	ComputedAt         *time.Time
}

// A sample can change its own interval and the previous interval's endpoint.
// The 15s cap limits invalidation to at most two adjacent closed hour buckets.
func dirtyAvailability(tx *gorm.DB, deviceID, ts int64) error {
	return tx.Exec(`INSERT INTO availability_hourly(device_id,hour_ms)
 SELECT ?,h FROM generate_series(?,?,3600000::bigint) h
 WHERE h < (floor(extract(epoch FROM clock_timestamp())/3600)*3600000)::bigint
 ON CONFLICT(device_id,hour_ms,rule_version) DO UPDATE SET generation=availability_hourly.generation+1`,
		deviceID, (ts-maxIntervalMs)/availabilityHour*availabilityHour, (ts+maxIntervalMs)/availabilityHour*availabilityHour).Error
}
func availabilityDirtyCTE(deviceID, ts int64) string {
	return availabilityDirtyCTEFor(deviceID, ts, "confirmed")
}
func availabilityDirtyCTEFor(deviceID, ts int64, source string) string {
	if source != "confirmed" && source != "inserted" {
		panic("invalid availability source")
	}
	return fmt.Sprintf(`report_dirty AS(INSERT INTO availability_hourly(device_id,hour_ms)
 SELECT c.device_id,h FROM %s c CROSS JOIN generate_series(%d::bigint,%d::bigint,3600000::bigint) h
 WHERE c.device_id=%d AND h<(floor(extract(epoch FROM clock_timestamp())/3600)*3600000)::bigint
 ON CONFLICT(device_id,hour_ms,rule_version) DO UPDATE SET generation=availability_hourly.generation+1 RETURNING device_id),
 `, source, (ts-maxIntervalMs)/availabilityHour*availabilityHour, (ts+maxIntervalMs)/availabilityHour*availabilityHour, deviceID)
}

// Reuse the raw interval rule while splitting durations exactly at hour edges.
type hourlyFold struct {
	start, end  int64
	previous    samplePoint
	hasPrevious bool
	rows        map[int64]availabilityResult
}

func newHourlyFold(start, end int64) *hourlyFold {
	f := &hourlyFold{start: start, end: end, rows: map[int64]availabilityResult{}}
	for h := start; h < end; h += availabilityHour {
		f.rows[h] = availabilityResult{WindowMs: availabilityHour, RuleVersion: availabilityRule}
	}
	return f
}
func (f *hourlyFold) add(p samplePoint) {
	if f.hasPrevious {
		f.close(p.TS)
	}
	f.previous = p
	f.hasPrevious = true
}
func (f *hourlyFold) close(next int64) {
	left := max(f.previous.TS, f.start)
	right := min3(f.previous.TS+maxIntervalMs, next, f.end)
	for left < right {
		h := left / availabilityHour * availabilityHour
		until := min(right, h+availabilityHour)
		r := f.rows[h]
		duration := until - left
		r.CoveredMs += duration
		switch f.previous.Status {
		case 1:
			r.RunningMs += duration
		case 0:
			r.StoppedMs += duration
		case 2:
			r.FaultMs += duration
		}
		f.rows[h] = r
		left = until
	}
}
func (f *hourlyFold) finish() map[int64]availabilityResult {
	if f.hasPrevious {
		f.close(f.end)
		f.hasPrevious = false
	}
	for h, r := range f.rows {
		r.UnknownMs = r.WindowMs - r.CoveredMs
		f.rows[h] = r
	}
	return f.rows
}

func (s *Server) availabilityRollupWorker(ctx context.Context) {
	ticker := time.NewTicker(30 * time.Second)
	defer ticker.Stop()
	for ctx.Err() == nil {
		if err := s.AvailabilityRollupStep(ctx, 20*time.Second); err != nil && ctx.Err() == nil {
			s.telemetryLog("availability_rollup", time.Now())
		}
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}

// Independent, bounded worker. It holds only its own advisory lock, never an
// ingestion/device lock, while two readers scan TDengine. Rebuilds are derived.
func (s *Server) AvailabilityRollupStep(parent context.Context, budget time.Duration) error {
	ctx, cancel := context.WithTimeout(parent, budget)
	defer cancel()
	return s.db.WithContext(ctx).Transaction(func(lock *gorm.DB) error {
		var held bool
		if err := lock.Raw("SELECT pg_try_advisory_xact_lock(?)", availabilityRollupLock).Scan(&held).Error; err != nil {
			return err
		}
		if !held {
			return nil
		}
		var state rollupState
		if err := s.db.WithContext(ctx).Raw("SELECT * FROM availability_rollup_state WHERE id=1").Scan(&state).Error; err != nil {
			return err
		}
		if !state.WorkerEnabled {
			return nil
		}
		closed := time.Now().UTC().Truncate(time.Hour).UnixMilli()
		var missingDevice bool
		if err := s.db.WithContext(ctx).Raw(`SELECT EXISTS(SELECT 1 FROM devices d WHERE d.deleted_at IS NULL AND d.created_at<to_timestamp(?/1000.0) AND NOT EXISTS(SELECT 1 FROM availability_hourly h WHERE h.device_id=d.id AND h.rule_version=?))`, closed, availabilityRule).Scan(&missingDevice).Error; err != nil {
			return err
		}
		if state.SeededHourMs < closed || missingDevice {
			if err := s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
				if err := tx.Exec(`INSERT INTO availability_hourly(device_id,hour_ms)
 SELECT d.id,h FROM devices d CROSS JOIN LATERAL generate_series(
 greatest((floor(extract(epoch FROM d.created_at)/3600)*3600000)::bigint,?::bigint),?::bigint,3600000::bigint) h
 WHERE d.deleted_at IS NULL AND d.created_at<to_timestamp(?/1000.0)
 ON CONFLICT(device_id,hour_ms,rule_version) DO NOTHING`, closed-365*24*availabilityHour, closed-availabilityHour, closed).Error; err != nil {
					return err
				}
				return tx.Exec("UPDATE availability_rollup_state SET seeded_hour_ms=? WHERE id=1", closed).Error
			}); err != nil {
				return err
			}
		}
		var jobs []struct {
			DeviceID int64
			HourMs   int64
		}
		if err := s.db.WithContext(ctx).Raw(`SELECT h.device_id,max(h.hour_ms) hour_ms FROM availability_hourly h JOIN devices d ON d.id=h.device_id
 WHERE d.deleted_at IS NULL AND h.hour_ms<? AND h.rule_version=? AND h.generation<>h.computed_generation
 GROUP BY h.device_id ORDER BY max(CASE WHEN h.computed_at IS NOT NULL THEN 1 ELSE 0 END) DESC,max(h.hour_ms) DESC,h.device_id`, closed, availabilityRule).Scan(&jobs).Error; err != nil {
			return err
		}
		queue := make(chan int)
		var wg sync.WaitGroup
		var mu sync.Mutex
		var first error
		for n := 0; n < 2; n++ {
			wg.Add(1)
			go func() {
				defer wg.Done()
				for i := range queue {
					if ctx.Err() != nil {
						continue
					}
					if err := s.rebuildAvailabilityHours(ctx, jobs[i].DeviceID, jobs[i].HourMs); err != nil {
						mu.Lock()
						if first == nil {
							first = err
						}
						mu.Unlock()
					}
				}
			}()
		}
		for i := range jobs {
			select {
			case queue <- i:
			case <-ctx.Done():
			}
		}
		close(queue)
		wg.Wait()
		// A bounded sweep may leave backfill work. Only successful scans publish rows;
		// query-time generation/count checks reject any unfinished buckets.
		if ctx.Err() != nil {
			return ctx.Err()
		}
		if first != nil {
			return first
		}
		return s.db.WithContext(ctx).Exec("UPDATE availability_rollup_state SET swept_at=clock_timestamp() WHERE id=1").Error
	})
}
func (s *Server) rebuildAvailabilityHours(ctx context.Context, deviceID, anchor int64) error {
	var targets []hourlyAvailability
	if err := s.db.WithContext(ctx).Raw(`SELECT * FROM availability_hourly WHERE device_id=? AND rule_version=? AND hour_ms BETWEEN ? AND ? AND generation<>computed_generation ORDER BY hour_ms`, deviceID, availabilityRule, anchor-23*availabilityHour, anchor).Scan(&targets).Error; err != nil {
		return err
	}
	if len(targets) == 0 {
		return nil
	}
	var device model.Device
	if err := s.db.WithContext(ctx).Where("id=? AND deleted_at IS NULL", deviceID).First(&device).Error; err != nil {
		return err
	}
	start, end := targets[0].HourMs, targets[len(targets)-1].HourMs+availabilityHour
	fold := newHourlyFold(start, end)
	if err := s.scanAvailability(ctx, device, start, end, fold.add); err != nil {
		return err
	}
	rows := fold.finish()
	// Compare-and-swap each generation. A concurrently received late sample leaves
	// its bucket dirty, even if this scan happened to observe the newer TD row.
	return s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		for _, target := range targets {
			r := rows[target.HourMs]
			if err := tx.Exec(`UPDATE availability_hourly SET covered_ms=?,running_ms=?,stopped_ms=?,fault_ms=?,unknown_ms=?,computed_at=clock_timestamp(),computed_generation=?
 WHERE device_id=? AND hour_ms=? AND rule_version=? AND generation=?`, r.CoveredMs, r.RunningMs, r.StoppedMs, r.FaultMs, r.UnknownMs, target.Generation, deviceID, target.HourMs, availabilityRule, target.Generation).Error; err != nil {
				return err
			}
		}
		return nil
	})
}

type availabilityMetadata struct {
	UpdatedAt string `json:"statistics_updated_at"`
	State     string `json:"aggregation_state"`
	LagMs     int64  `json:"aggregation_lag_ms"`
}

func rawAvailabilityMetadata(state string) availabilityMetadata {
	return availabilityMetadata{UpdatedAt: time.Now().UTC().Format(time.RFC3339Nano), State: state}
}

type availabilitySums struct {
	DeviceID  int64
	Buckets   int64
	Dirty     int64
	CoveredMs int64
	RunningMs int64
	StoppedMs int64
	FaultMs   int64
	UnknownMs int64
}

func fullHourRange(start, end int64) (int64, int64) {
	return (start + availabilityHour - 1) / availabilityHour * availabilityHour, end / availabilityHour * availabilityHour
}

// The raw oracle stays available. The fast path never uses an unbuilt/dirty
// bucket, a partial device list, or a worker heartbeat older than one minute.
func (s *Server) availabilityReportDevices(ctx context.Context, devices []model.Device, start, end time.Time) ([]availabilityResult, availabilityMetadata, error) {
	raw := func(state string) ([]availabilityResult, availabilityMetadata, error) {
		rows, err := s.availabilityDevices(ctx, devices, start, end)
		return rows, rawAvailabilityMetadata(state), err
	}
	if !s.rollupInstalled {
		return raw("raw")
	}
	var state rollupState
	if err := s.db.WithContext(ctx).Raw("SELECT * FROM availability_rollup_state WHERE id=1").Scan(&state).Error; err != nil {
		return raw("degraded")
	}
	if !state.ReadEnabled {
		return raw("warming")
	}
	if state.SweptAt == nil || time.Since(*state.SweptAt) > time.Minute || time.Until(*state.SweptAt) > time.Second {
		return raw("degraded")
	}
	if len(devices) == 0 {
		return []availabilityResult{}, rawAvailabilityMetadata("ready"), nil
	}
	values := make([]string, 0, len(devices))
	args := make([]any, 0, len(devices)*3+1)
	for _, d := range devices {
		from, to := fullHourRange(max(start.UnixMilli(), d.CreatedAt.UnixMilli()), end.UnixMilli())
		values = append(values, "(?::bigint,?::bigint,?::bigint)")
		args = append(args, d.ID, from, to)
	}
	args = append(args, availabilityRule)
	var sums []availabilitySums
	if err := s.db.WithContext(ctx).Raw(`WITH ranges(device_id,lo,hi) AS(VALUES `+strings.Join(values, ",")+`)
 SELECT r.device_id,count(h.hour_ms) buckets,
 count(h.hour_ms) FILTER(WHERE h.computed_at IS NULL OR h.generation<>h.computed_generation) dirty,
 coalesce(sum(h.covered_ms),0) covered_ms,coalesce(sum(h.running_ms),0) running_ms,
 coalesce(sum(h.stopped_ms),0) stopped_ms,coalesce(sum(h.fault_ms),0) fault_ms,coalesce(sum(h.unknown_ms),0) unknown_ms
 FROM ranges r LEFT JOIN availability_hourly h ON h.device_id=r.device_id AND h.hour_ms>=r.lo AND h.hour_ms<r.hi AND h.rule_version=? GROUP BY r.device_id`, args...).Scan(&sums).Error; err != nil {
		return raw("degraded")
	}
	byID := map[int64]availabilitySums{}
	for _, r := range sums {
		byID[r.DeviceID] = r
	}
	results := make([]availabilityResult, len(devices))
	queue := make(chan int, len(devices))
	for i := range devices {
		queue <- i
	}
	close(queue)
	child, cancel := context.WithCancel(ctx)
	defer cancel()
	var wg sync.WaitGroup
	var once sync.Once
	var first error
	var fallback bool
	var fallbackMu sync.Mutex
	for n := 0; n < min(3, len(devices)); n++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := range queue {
				if child.Err() != nil {
					return
				}
				d := devices[i]
				lo := max(start.UnixMilli(), d.CreatedAt.UnixMilli())
				hi := end.UnixMilli()
				from, to := fullHourRange(lo, hi)
				sum, found := byID[d.ID]
				var r availabilityResult
				var err error
				if to <= from || !found || sum.Dirty > 0 || sum.Buckets != (to-from)/availabilityHour {
					r, err = s.loadAvailability(child, d, lo, hi)
					if to > from {
						fallbackMu.Lock()
						fallback = true
						fallbackMu.Unlock()
					}
				} else {
					r = availabilityResult{WindowMs: hi - lo, CoveredMs: sum.CoveredMs, RunningMs: sum.RunningMs, StoppedMs: sum.StoppedMs, FaultMs: sum.FaultMs, UnknownMs: sum.UnknownMs}
					for _, edge := range [][2]int64{{lo, from}, {to, hi}} {
						if edge[1] > edge[0] {
							var tail availabilityResult
							tail, err = s.loadAvailability(child, d, edge[0], edge[1])
							if err != nil {
								break
							}
							r.CoveredMs += tail.CoveredMs
							r.RunningMs += tail.RunningMs
							r.StoppedMs += tail.StoppedMs
							r.FaultMs += tail.FaultMs
							r.UnknownMs += tail.UnknownMs
						}
					}
					r = finalizeRollupAvailability(r)
				}
				if err != nil {
					once.Do(func() { first = err; cancel() })
					return
				}
				r.DeviceID = d.ID
				r.DeviceCode = d.DeviceCode
				r.StationCode = d.StationCode
				results[i] = r
			}
		}()
	}
	wg.Wait()
	if first != nil {
		return nil, availabilityMetadata{}, first
	}
	if ctx.Err() != nil {
		return nil, availabilityMetadata{}, ctx.Err()
	}
	meta := availabilityMetadata{UpdatedAt: state.SweptAt.UTC().Format(time.RFC3339Nano), State: "ready", LagMs: max(0, time.Since(*state.SweptAt).Milliseconds())}
	if meta.LagMs > 60_000 {
		return raw("degraded")
	}
	if fallback {
		meta.State = "warming"
	}
	return results, meta, nil
}
func finalizeRollupAvailability(r availabilityResult) availabilityResult {
	r.RuleVersion = availabilityRule
	if r.WindowMs <= 0 {
		r.Insufficient = true
		return r
	}
	coverage := float64(r.CoveredMs) / float64(r.WindowMs)
	r.DataCoverage = &coverage
	r.Insufficient = coverage < .9
	if r.CoveredMs > 0 {
		v := float64(r.RunningMs+r.StoppedMs) / float64(r.CoveredMs)
		r.ObservedAvail = &v
	} else {
		r.Insufficient = true
	}
	return r
}

// Local command support: deliberately no HTTP mutation endpoint or new role.
func (s *Server) SetAvailabilityRollupRead(ctx context.Context, enabled bool) error {
	if !s.rollupInstalled {
		return errors.New("availability_rollup_not_installed")
	}
	return s.db.WithContext(ctx).Exec("UPDATE availability_rollup_state SET read_enabled=? WHERE id=1", enabled).Error
}
