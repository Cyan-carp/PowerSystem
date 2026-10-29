package energy

import (
	"math"
	"reflect"
	"strings"
	"testing"
	"time"
)

func TestIntegrateDay(t *testing.T) {
	start := time.Date(2026, 9, 29, 0, 0, 0, 0, china).UnixMilli()
	for _, tc := range []struct {
		name   string
		points []Sample
		want   float64
	}{
		{"steady", []Sample{{start, 100}, {start + 5000, 100}}, 100.0 * 5 / 3600},
		{"gap omitted", []Sample{{start, 100}, {start + 16000, 100}}, 0},
		{"duplicate timestamp", []Sample{{start, 100}, {start, 100}, {start + 5000, 100}}, 100.0 * 5 / 3600},
		{"late out of order sample", []Sample{{start + 10000, 100}, {start, 100}, {start + 5000, 100}}, 100.0 * 10 / 3600},
		{"cross midnight", []Sample{{start - 5000, 100}, {start + 5000, 100}}, 100.0 * 5 / 3600},
		{"night zero", []Sample{{start, 0}, {start + 5000, 0}}, 0},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got := IntegrateDay(tc.points, start, start+24*60*60*1000)
			if math.Abs(got-tc.want) > 1e-9 {
				t.Fatalf("got %v want %v", got, tc.want)
			}
		})
	}
}

func TestAffectedDaysCoversLateMidnightSamples(t *testing.T) {
	for _, tc := range []struct {
		clock string
		want  []string
	}{
		{"2026-09-28 23:59:58", []string{"2026-09-28", "2026-09-29"}},
		{"2026-09-29 00:00:02", []string{"2026-09-28", "2026-09-29"}},
		{"2026-09-29 12:00:00", []string{"2026-09-29"}},
	} {
		stamp, err := time.ParseInLocation("2006-01-02 15:04:05", tc.clock, china)
		if err != nil {
			t.Fatal(err)
		}
		if got := affectedDays(stamp.UnixMilli()); !reflect.DeepEqual(got, tc.want) {
			t.Fatalf("%s: got %v want %v", tc.clock, got, tc.want)
		}
	}
}

func TestRecalculationIsRepeatable(t *testing.T) {
	points := []Sample{{10000, 100}, {0, 100}, {5000, 100}, {5000, 100}}
	first := IntegrateDay(points, 0, 15000)
	second := IntegrateDay(points, 0, 15000)
	if first != second || math.Abs(first-100.0*10/3600) > 1e-9 {
		t.Fatalf("rebuild changed result: first=%v second=%v", first, second)
	}
}

func TestDeviceScore(t *testing.T) {
	for _, tc := range []struct {
		online bool
		status int
		level  string
		want   int
	}{
		{false, 1, "", 0}, {true, 2, "", 0}, {true, 0, "", 100},
		{true, 1, "minor", 90}, {true, 1, "major", 70}, {true, 1, "urgent", 40},
	} {
		if got := DeviceScore(tc.online, tc.status, tc.level); got != tc.want {
			t.Fatalf("got %d want %d", got, tc.want)
		}
	}
}

func TestEmptyAndZeroEnergyAreDifferent(t *testing.T) {
	if _, segments := calculateDay([]Sample{{0, 0}}, 0, 10_000); segments != 0 {
		t.Fatalf("one sample unexpectedly formed %d segments", segments)
	}
	if value, segments := calculateDay([]Sample{{0, 0}, {5000, 0}}, 0, 10_000); segments != 1 || value != 0 {
		t.Fatalf("zero power should be valid: kwh=%v segments=%d", value, segments)
	}
}

func TestSummarizeWindowFoldsOrderedDay(t *testing.T) {
	const start, end = int64(0), int64(60_000)
	// 5s cadence, duplicate timestamp keeps the later row, one 15s boundary gap
	// still counts, one longer gap is counted and refused, and rows just outside
	// the window only contribute through their straddling segment.
	samples := []Sample{
		{start - 5000, 90},
		{start, 100},
		{start + 5000, 100},
		{start + 5000, 120},
		{start + 10000, 120},
		{start + 25000, 120},
		{end, 120},
		{end + 5000, 120},
	}
	got := summarizeWindow(samples, start, end)
	want := windowSummary{
		KWh:       (110.0*5 + 120.0*5 + 120.0*15) / 3600,
		Segments:  3,
		CoveredMS: 25_000,
		Gaps:      1,
		MaxGapMS:  35_000,
	}
	if got != want {
		t.Fatalf("fold: got %+v want %+v", got, want)
	}
}

func TestSummarizeWindowClipsToDayAndDropsEmpty(t *testing.T) {
	start, end := int64(1_000_000), int64(2_000_000)
	if got := summarizeWindow(nil, start, end); got != (windowSummary{}) {
		t.Fatalf("empty window: %+v", got)
	}
	// Both rows sit outside the day and their gap exceeds maxGapMs, so nothing
	// is integrated even though the window is inside the pair.
	got := summarizeWindow([]Sample{{start - 4000, 100}, {end + 4000, 100}}, start, end)
	if got != (windowSummary{Gaps: 1, MaxGapMS: 1_008_000}) {
		t.Fatalf("outside window contributed: %+v", got)
	}
	// A single straddling pair contributes exactly the clipped day, not the
	// full window between the samples.
	got = summarizeWindow([]Sample{{-2000, 60}, {12_000, 60}}, 0, 10_000)
	if got.Segments != 1 || got.CoveredMS != 10_000 {
		t.Fatalf("clipped coverage: %+v", got)
	}
}

// The device-day rebuild regressed to a bitmap union over device-wide partial
// indexes with heap-side date filtering plus a full-day count(*). Keep the
// guarded shapes so the plan regression cannot silently return.
func TestRebuildSQLKeepsDaySliceIndexAndExistenceProbe(t *testing.T) {
	if strings.Contains(strings.ToUpper(sourceSamplesSQL), " OR ") {
		t.Fatal("source query reintroduced a disjunction that defeats idx_inbox_energy")
	}
	for _, want := range []string{"COALESCE(business_state,'success')='success'", "device_id=?", "ts_ms>=?", "ts_ms<=?", "ORDER BY ts_ms,id"} {
		if !strings.Contains(sourceSamplesSQL, want) {
			t.Fatalf("source query lost %q", want)
		}
	}
	if strings.Contains(strings.ToUpper(uncertainExistsSQL), "COUNT(") {
		t.Fatal("uncertainty probe reintroduced a full-day count")
	}
	if !strings.Contains(uncertainExistsSQL, "EXISTS(SELECT 1") || !strings.Contains(uncertainExistsSQL, "business_state IS DISTINCT FROM 'success'") {
		t.Fatalf("uncertainty probe changed shape: %s", uncertainExistsSQL)
	}
}
