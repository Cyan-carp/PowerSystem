package api

import (
	"context"
	"encoding/json"
	"os"
	"reflect"
	"testing"
	"time"

	"powersystem/backend/internal/config"
	"powersystem/backend/internal/model"
	"powersystem/backend/internal/tdengine"
)

// Opt-in read comparison against an explicitly supplied server baseline. This
// starts no API, ingestion, workers or outbound model requests.
func TestReportServerBenchmark(t *testing.T) {
	input := os.Getenv("REPORT_PERFORMANCE_INPUT")
	if input == "" {
		t.Skip("requires explicit private baseline and server TD configuration")
	}
	raw, err := os.ReadFile(input)
	if err != nil {
		t.Fatal(err)
	}
	var baseline struct {
		Data struct {
			Start, End time.Time
			Devices    []availabilityResult
			Stations   []stationAvailability
		}
	}
	if err := json.Unmarshal(raw, &baseline); err != nil {
		t.Fatal(err)
	}
	cfg := config.Config{TDURL: os.Getenv("TDENGINE_URL"), TDUser: os.Getenv("TDENGINE_USER"), TDPassword: os.Getenv("TDENGINE_ROOT_PASSWORD"), TDDatabase: os.Getenv("TDENGINE_DATABASE")}
	if cfg.TDURL == "" || cfg.TDDatabase == "" {
		t.Fatal("explicit TD configuration required")
	}
	devices := make([]model.Device, len(baseline.Data.Devices))
	for i, d := range baseline.Data.Devices {
		devices[i] = model.Device{ID: d.DeviceID, DeviceCode: d.DeviceCode, StationCode: d.StationCode}
		devices[i].CreatedAt = baseline.Data.End.Add(-time.Duration(d.WindowMs) * time.Millisecond)
	}
	s := &Server{td: tdengine.New(cfg)}
	var seconds []float64
	for trial := 0; trial < 3; trial++ {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
		begin := time.Now()
		got, err := s.availabilityDevices(ctx, devices, baseline.Data.Start, baseline.Data.End)
		cancel()
		if err != nil {
			t.Fatal(err)
		}
		seconds = append(seconds, time.Since(begin).Seconds())
		if !reflect.DeepEqual(got, baseline.Data.Devices) || !reflect.DeepEqual(s.aggregateAvailability(got), baseline.Data.Stations) {
			t.Fatal("candidate differs from baseline durations/ratios/completeness")
		}
	}
	result := map[string]any{"exact_results_equal": true, "trials_seconds": seconds, "devices": len(devices), "window_days": 7}
	out, err := json.MarshalIndent(result, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(os.Getenv("REPORT_PERFORMANCE_OUTPUT"), out, 0600); err != nil {
		t.Fatal(err)
	}
	t.Log(string(out))
}
