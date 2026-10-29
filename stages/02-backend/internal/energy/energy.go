package energy

import (
	"context"
	"database/sql"
	"errors"
	"math"
	"sort"
	"time"

	"gorm.io/gorm"
)

const maxGapMs int64 = 15_000

var china = time.FixedZone("Asia/Shanghai", 8*60*60)

type Sample struct {
	TS    int64
	Power float64
}

type Summary struct {
	Provisional         bool
	TodayProvisional    bool
	RetainedProvisional bool
	TodayKWh            *float64
	RetainedKWh         *float64
	StartMS             *int64

	UpdatedAt *time.Time
}

func dayFor(ts int64) string { return time.UnixMilli(ts).In(china).Format("2006-01-02") }

// MarkDirty runs in the same transaction that marks an inbox sample processed.
// Either adjacent date may receive a short segment when a late sample arrives.
func MarkDirty(tx *gorm.DB, deviceID, ts int64) error {
	for _, day := range affectedDays(ts) {
		if err := tx.Exec(`INSERT INTO energy_dirty_days(device_id,day,changed_at) VALUES(?,?,clock_timestamp())
ON CONFLICT(device_id,day) DO UPDATE SET changed_at=clock_timestamp()`, deviceID, day).Error; err != nil {
			return err
		}
	}
	return nil
}

func affectedDays(ts int64) []string {
	days := make([]string, 0, 3)
	seen := make(map[string]bool, 3)
	for _, offset := range []int64{-maxGapMs, 0, maxGapMs} {
		day := dayFor(ts + offset)
		if !seen[day] {
			seen[day] = true
			days = append(days, day)
		}
	}
	return days
}

// IntegrateDay uses linear interpolation between adjacent, distinct samples.
// Gaps longer than 15 seconds are omitted rather than extrapolated.
func IntegrateDay(samples []Sample, start, end int64) float64 {
	energy, _ := calculateDay(samples, start, end)
	return energy
}

func calculateDay(samples []Sample, start, end int64) (float64, int) {
	samples = normalizeSamples(samples)
	energy := 0.0
	segments := 0
	for i := 1; i < len(samples); i++ {
		a, b := samples[i-1], samples[i]
		gap := b.TS - a.TS
		if gap <= 0 || gap > maxGapMs || math.IsNaN(a.Power) || math.IsNaN(b.Power) {
			continue
		}
		left, right := max(a.TS, start), min(b.TS, end)
		if right <= left {
			continue
		}
		pLeft := a.Power + (b.Power-a.Power)*float64(left-a.TS)/float64(gap)
		pRight := a.Power + (b.Power-a.Power)*float64(right-a.TS)/float64(gap)
		energy += (pLeft + pRight) / 2 * float64(right-left) / 3_600_000
		segments++
	}
	return energy, segments
}

func normalizeSamples(input []Sample) []Sample {
	samples := append([]Sample(nil), input...)
	sort.SliceStable(samples, func(i, j int) bool { return samples[i].TS < samples[j].TS })
	unique := samples[:0]
	for _, sample := range samples {
		if len(unique) > 0 && unique[len(unique)-1].TS == sample.TS {
			unique[len(unique)-1] = sample
		} else {
			unique = append(unique, sample)
		}
	}
	return unique
}

type dirtyDay struct {
	DeviceID  int64
	Day       string
	ChangedAt time.Time
}

// sourceSamplesSQL intentionally contains no disjunction on business_state.
// With `business_state='success' OR business_state IS NULL` the planner
// bitmap-unioned the device-wide partial indexes (idx_inbox_restore /
// idx_inbox_success) and then filtered ts_ms on the heap, reading a whole
// device history instead of one day. COALESCE keeps the exact same row set and
// lets idx_inbox_energy (device_id,ts_ms,id) WHERE processed_at IS NOT NULL
// serve device and day range as index conditions in index order.
const sourceSamplesSQL = `SELECT ts_ms,(payload->>'power')::double precision AS power
FROM telemetry_inbox WHERE device_id=? AND processed_at IS NOT NULL AND COALESCE(business_state,'success')='success'
AND ts_ms>=? AND ts_ms<=? ORDER BY ts_ms,id`

// uncertainExistsSQL only answers "is any sample not success". The previous
// count(*) could not use any index for the state predicate and therefore
// fetched every row of the device-day on each rebuild; EXISTS stops at the
// first such row and returns the same verdict.
const uncertainExistsSQL = `SELECT EXISTS(SELECT 1 FROM telemetry_inbox WHERE device_id=? AND ts_ms>=? AND ts_ms<=? AND business_state IS DISTINCT FROM 'success')`

// RebuildPending refreshes a bounded number of dirty days. A concurrent new
// sample keeps its dirty marker so the next pass corrects the daily total.
// One slow or failing day must not starve the rest of the queue: completed
// days are already committed by rebuildDay, so the round continues and the
// first error is reported afterwards.
func RebuildPending(ctx context.Context, db *gorm.DB, limit int) (int, error) {
	var days []dirtyDay
	if err := db.WithContext(ctx).Raw(`SELECT device_id,day::text AS day,changed_at
FROM energy_dirty_days ORDER BY changed_at LIMIT ?`, limit).Scan(&days).Error; err != nil {
		return 0, err
	}
	rebuilt := 0
	var firstErr error
	for _, d := range days {
		if err := rebuildDay(ctx, db, d); err != nil {
			if firstErr == nil {
				firstErr = err
			}
			continue
		}
		rebuilt++
	}
	return rebuilt, firstErr
}

func rebuildDay(ctx context.Context, db *gorm.DB, d dirtyDay) error {
	date, err := time.ParseInLocation("2006-01-02", d.Day, china)
	if err != nil {
		return err
	}
	start, end := date.UnixMilli(), date.AddDate(0, 0, 1).UnixMilli()
	rows, err := db.WithContext(ctx).Raw(sourceSamplesSQL, d.DeviceID, start-maxGapMs, end+maxGapMs).Rows()
	if err != nil {
		return err
	}
	defer rows.Close()
	samples := make([]Sample, 0, 64)
	for rows.Next() {
		var sample Sample
		if err := rows.Scan(&sample.TS, &sample.Power); err != nil {
			return err
		}
		samples = append(samples, sample)
	}
	if err := rows.Err(); err != nil {
		return err
	}
	window := summarizeWindow(samples, start, end)
	var uncertain bool
	if err := db.WithContext(ctx).Raw(uncertainExistsSQL, d.DeviceID, start-maxGapMs, end+maxGapMs).Row().Scan(&uncertain); err != nil {
		return err
	}
	quality := "verified"
	if uncertain || window.Gaps > 0 {
		quality = "provisional"
	}
	return db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		// The same singleton UPDATE is the drain barrier for derived energy writes.
		if tx.Dialector.Name() == "postgres" {
			var gate struct{ Enabled bool }
			r := tx.Raw("SELECT enabled FROM alarm_maintenance_state WHERE id=1 FOR SHARE").Scan(&gate)
			if r.Error != nil {
				return r.Error
			}
			if r.RowsAffected != 1 || gate.Enabled {
				return errors.New("energy_maintenance_closed")
			}
		}
		if err := tx.Exec(`INSERT INTO energy_daily(device_id,day,kwh,segments_count,source_quality,covered_ms,gap_count,max_gap_ms,updated_at) VALUES(?,?,?,?,?,?,?,?,now())
ON CONFLICT(device_id,day) DO UPDATE SET kwh=excluded.kwh,segments_count=excluded.segments_count,source_quality=excluded.source_quality,covered_ms=excluded.covered_ms,gap_count=excluded.gap_count,max_gap_ms=excluded.max_gap_ms,updated_at=now()`, d.DeviceID, d.Day, window.KWh, window.Segments, quality, window.CoveredMS, window.Gaps, window.MaxGapMS).Error; err != nil {
			return err
		}
		return tx.Exec(`DELETE FROM energy_dirty_days WHERE device_id=? AND day=? AND changed_at<=?`, d.DeviceID, d.Day, d.ChangedAt).Error
	})
}

// windowSummary is the persisted outcome of folding one device-day.
type windowSummary struct {
	KWh       float64
	Segments  int
	CoveredMS int64
	Gaps      int64
	MaxGapMS  int64
}

// summarizeWindow folds a device-day window. Input must be ordered by ts_ms and
// may extend one maxGapMs past the day on either side so that boundary
// segments interpolate; rows outside the day only contribute through those
// segments. Duplicate timestamps keep the later row.
func summarizeWindow(samples []Sample, start, end int64) windowSummary {
	var out windowSummary
	var left, right *Sample
	accumulate := func() {
		if left == nil || right == nil || min(right.TS, end) <= max(left.TS, start) {
			return
		}
		gap := right.TS - left.TS
		if gap > out.MaxGapMS {
			out.MaxGapMS = gap
		}
		if gap > maxGapMs {
			out.Gaps++
			return
		}
		value, n := calculateDay([]Sample{*left, *right}, start, end)
		out.KWh += value
		out.Segments += n
		if n > 0 {
			out.CoveredMS += min(right.TS, end) - max(left.TS, start)
		}
	}
	for i := range samples {
		sample := &samples[i]
		if right == nil {
			right = sample
			continue
		}
		if sample.TS == right.TS {
			right = sample
			continue
		}
		accumulate()
		left = right
		right = sample
	}
	accumulate()
	return out
}

func ReadSummary(ctx context.Context, db *gorm.DB, now time.Time) (Summary, error) {
	var todayValue, retainedValue sql.NullFloat64
	var updated sql.NullTime
	today := now.In(china).Format("2006-01-02")
	if err := db.WithContext(ctx).Raw(`SELECT SUM(CASE WHEN day=? AND segments_count>0 THEN kwh END),
SUM(CASE WHEN segments_count>0 THEN kwh END), MAX(updated_at) FROM energy_daily`, today).Row().Scan(&todayValue, &retainedValue, &updated); err != nil {
		return Summary{}, err
	}
	var uncertainty struct {
		Today    int64
		Retained int64
	}
	if err := db.WithContext(ctx).Raw("SELECT COALESCE(SUM(CASE WHEN day=? AND source_quality<>'verified' THEN 1 ELSE 0 END),0)+(SELECT count(*) FROM energy_dirty_days WHERE day=?) AS today, COALESCE(SUM(CASE WHEN source_quality<>'verified' THEN 1 ELSE 0 END),0)+(SELECT count(*) FROM energy_dirty_days) AS retained FROM energy_daily", today, today).Scan(&uncertainty).Error; err != nil {
		return Summary{}, err
	}
	var first sql.NullInt64
	if err := db.WithContext(ctx).Raw(`SELECT MIN(ts_ms) FROM telemetry_inbox WHERE processed_at IS NOT NULL AND (business_state='success' OR business_state IS NULL)`).Row().Scan(&first); err != nil {
		return Summary{}, err
	}
	if !first.Valid {
		return Summary{}, nil
	}
	s := Summary{StartMS: &first.Int64, Provisional: uncertainty.Retained > 0, TodayProvisional: uncertainty.Today > 0 || !todayValue.Valid, RetainedProvisional: uncertainty.Retained > 0 || !retainedValue.Valid}
	if todayValue.Valid {
		s.TodayKWh = &todayValue.Float64
	}
	if retainedValue.Valid {
		s.RetainedKWh = &retainedValue.Float64
	}
	if updated.Valid {
		s.UpdatedAt = &updated.Time
	}
	return s, nil
}

func DeviceScore(online bool, status int, level string) int {
	if !online || status == 2 {
		return 0
	}
	switch level {
	case "urgent":
		return 40
	case "major":
		return 70
	case "minor":
		return 90
	default:
		return 100
	}
}
