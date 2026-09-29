package energy

import (
	"context"
	"database/sql"
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
	TodayKWh    *float64
	RetainedKWh *float64
	StartMS     *int64
	UpdatedAt   *time.Time
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

// RebuildPending refreshes a bounded number of dirty days. A concurrent new
// sample keeps its dirty marker so the next pass corrects the daily total.
func RebuildPending(ctx context.Context, db *gorm.DB, limit int) (int, error) {
	var days []dirtyDay
	if err := db.WithContext(ctx).Raw(`SELECT device_id,day::text AS day,changed_at
FROM energy_dirty_days ORDER BY changed_at LIMIT ?`, limit).Scan(&days).Error; err != nil {
		return 0, err
	}
	for _, d := range days {
		if err := rebuildDay(ctx, db, d); err != nil {
			return 0, err
		}
	}
	return len(days), nil
}

func rebuildDay(ctx context.Context, db *gorm.DB, d dirtyDay) error {
	date, err := time.ParseInLocation("2006-01-02", d.Day, china)
	if err != nil {
		return err
	}
	start, end := date.UnixMilli(), date.AddDate(0, 0, 1).UnixMilli()
	rows, err := db.WithContext(ctx).Raw(`SELECT ts_ms,(payload->>'power')::double precision AS power
FROM telemetry_inbox WHERE device_id=? AND processed_at IS NOT NULL
AND ts_ms>=? AND ts_ms<=? ORDER BY ts_ms,id`, d.DeviceID, start-maxGapMs, end+maxGapMs).Rows()
	if err != nil {
		return err
	}
	defer rows.Close()
	var samples []Sample
	for rows.Next() {
		var s Sample
		if err := rows.Scan(&s.TS, &s.Power); err != nil {
			return err
		}
		if len(samples) > 0 && samples[len(samples)-1].TS == s.TS {
			samples[len(samples)-1] = s
		} else {
			samples = append(samples, s)
		}
	}
	if err := rows.Err(); err != nil {
		return err
	}
	kwh, segments := calculateDay(samples, start, end)
	return db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		if err := tx.Exec(`INSERT INTO energy_daily(device_id,day,kwh,segments_count,updated_at) VALUES(?,?,?,?,now())
ON CONFLICT(device_id,day) DO UPDATE SET kwh=excluded.kwh,segments_count=excluded.segments_count,updated_at=now()`, d.DeviceID, d.Day, kwh, segments).Error; err != nil {
			return err
		}
		return tx.Exec(`DELETE FROM energy_dirty_days WHERE device_id=? AND day=? AND changed_at<=?`, d.DeviceID, d.Day, d.ChangedAt).Error
	})
}

func ReadSummary(ctx context.Context, db *gorm.DB, now time.Time) (Summary, error) {
	var todayValue, retainedValue sql.NullFloat64
	var updated sql.NullTime
	today := now.In(china).Format("2006-01-02")
	if err := db.WithContext(ctx).Raw(`SELECT SUM(CASE WHEN day=? AND segments_count>0 THEN kwh END),
SUM(CASE WHEN segments_count>0 THEN kwh END), MAX(updated_at) FROM energy_daily`, today).Row().Scan(&todayValue, &retainedValue, &updated); err != nil {
		return Summary{}, err
	}
	var first sql.NullInt64
	if err := db.WithContext(ctx).Raw(`SELECT MIN(ts_ms) FROM telemetry_inbox WHERE processed_at IS NOT NULL`).Row().Scan(&first); err != nil {
		return Summary{}, err
	}
	if !first.Valid {
		return Summary{}, nil
	}
	s := Summary{StartMS: &first.Int64}
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
