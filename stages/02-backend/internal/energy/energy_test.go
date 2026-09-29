package energy

import (
	"math"
	"reflect"
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
