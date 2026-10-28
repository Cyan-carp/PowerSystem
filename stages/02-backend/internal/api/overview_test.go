package api

import (
	"encoding/json"
	"testing"
	"time"

	"powersystem/backend/internal/model"
)

func overviewRaw(t *testing.T, now time.Time, power float64, status int, age time.Duration) []byte {
	t.Helper()
	received := now.Add(-age)
	payload := map[string]any{"received_at": received, "ts_ms": received.UnixMilli(), "power": power, "status": status}
	raw, err := json.Marshal(payload)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func TestAccumulateOverviewKeepsMissingValuesUnknown(t *testing.T) {
	now := time.Date(2026, 10, 20, 12, 0, 0, 0, time.UTC)
	devices := []model.Device{
		{ID: 1, DeviceCode: "A", StationCode: "PV-01"},
		{ID: 2, DeviceCode: "B", StationCode: "PV-01"},
		{ID: 3, DeviceCode: "C", StationCode: "PV-01"},
	}
	updated := now.Add(-time.Minute)
	acc := accumulateOverview(devices,
		map[int64][]byte{1: overviewRaw(t, now, 55.5, 0, time.Second), 2: overviewRaw(t, now, 20, 2, time.Second)},
		map[int64]overviewAlarm{1: {Count: 2, Level: "major"}, 2: {Count: 1, Level: "urgent"}},
		map[int64]overviewEnergy{1: {KWh: floatPtr(12.25), UpdatedAt: &updated}}, now)
	got := acc.summary()
	if acc.observed != 2 || got.Online != 2 || got.Offline != 0 || got.TelemetryUnknown != 1 || got.Fault != 1 || got.ActiveAlarms != 3 {
		t.Fatalf("unexpected counts: acc=%+v summary=%+v", acc, got)
	}
	if got.CurrentPowerKW == nil || *got.CurrentPowerKW != 75.5 {
		t.Fatalf("power=%v", got.CurrentPowerKW)
	}
	if got.TodayEnergyKWh == nil || *got.TodayEnergyKWh != 12.25 {
		t.Fatalf("energy=%v", got.TodayEnergyKWh)
	}
	if got.HealthValidCount != 2 || got.HealthUnknownCount != 1 || !got.HealthProvisional {
		t.Fatalf("health=%+v", got)
	}
}

func TestAccumulateOverviewDoesNotZeroFillAbsentMeasurements(t *testing.T) {
	now := time.Now()
	acc := accumulateOverview([]model.Device{{ID: 1, DeviceCode: "A"}}, nil, nil, nil, now)
	got := acc.summary()
	if got.CurrentPowerKW != nil || got.TodayEnergyKWh != nil || got.HealthScorePercent != nil {
		t.Fatalf("missing measurements must remain null: %+v", got)
	}
	if got.OperationalHealthPercent == nil || *got.OperationalHealthPercent != 0 {
		t.Fatalf("registered but unobserved operational health should be 0: %+v", got)
	}
}

func TestSnapshotCurrentRequiresFreshCompleteObservation(t *testing.T) {
	now := time.Now().UTC()
	cases := []struct {
		name string
		raw  []byte
		want bool
	}{
		{"fresh", overviewRaw(t, now, 10, 0, time.Second), true},
		{"stale", overviewRaw(t, now, 10, 0, 16*time.Second), false},
		{"missing power", []byte(`{"received_at":"2026-10-20T12:00:00Z","ts_ms":1792497600000,"status":0}`), false},
		{"invalid status", overviewRaw(t, now, 10, 3, time.Second), false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var sample overviewSnapshot
			if json.Unmarshal(tc.raw, &sample) != nil {
				t.Fatal("invalid test payload")
			}
			if got := snapshotIsCurrent(sample, now); got != tc.want {
				t.Fatalf("current=%v want %v", got, tc.want)
			}
		})
	}
}

func TestOverviewStationSerializesFlattenedSummary(t *testing.T) {
	value := 12.5
	raw, err := json.Marshal(overviewStation{
		StationCode: "PV-01", ExpectedDeviceCount: 48,
		overviewSummary: overviewSummary{DeviceTotal: 40, CurrentPowerKW: &value},
	})
	if err != nil {
		t.Fatal(err)
	}
	var fields map[string]any
	if err := json.Unmarshal(raw, &fields); err != nil {
		t.Fatal(err)
	}
	if fields["station_code"] != "PV-01" || fields["device_total"] != float64(40) || fields["current_power_kw"] != 12.5 {
		t.Fatalf("flattened station summary missing: %s", raw)
	}
}

func floatPtr(value float64) *float64 { return &value }
