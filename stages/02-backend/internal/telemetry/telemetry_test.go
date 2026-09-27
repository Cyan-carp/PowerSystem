package telemetry

import (
	"encoding/json"
	"testing"
	"time"
)

func TestParseRejectsMalformedAndPhysicallyInconsistentSamples(t *testing.T) {
	base := Sample{SchemaVersion: 1, RunID: "test_run", DeviceCode: "INV-1001", StationCode: "ST-01", Seq: 0, TS: time.Now().UnixMilli(), Voltage: 400, Current: 10, Temperature: 60, Power: 6.86, Status: 1}
	tests := []struct {
		name   string
		mutate func(*Sample)
	}{
		{"future timestamp", func(s *Sample) { s.TS = time.Now().Add(49 * time.Hour).UnixMilli() }},
		{"unsafe device code", func(s *Sample) { s.DeviceCode = "INV-1';DROP" }},
		{"impossible temperature", func(s *Sample) { s.Temperature = 120 }},
		{"inconsistent power", func(s *Sample) { s.Power = 90 }},
		{"invalid status", func(s *Sample) { s.Status = 8 }},
	}
	payload, _ := json.Marshal(base)
	if _, err := Parse(payload); err != nil {
		t.Fatalf("valid sample rejected: %v", err)
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			sample := base
			tc.mutate(&sample)
			payload, _ := json.Marshal(sample)
			if _, err := Parse(payload); err == nil {
				t.Fatal("invalid sample accepted")
			}
		})
	}
	if _, err := Parse([]byte(`{"device_id":"INV-1001"`)); err == nil {
		t.Fatal("malformed JSON accepted")
	}
	var missing map[string]any
	if err := json.Unmarshal(payload, &missing); err != nil {
		t.Fatal(err)
	}
	delete(missing, "fault_code")
	withoutField, _ := json.Marshal(missing)
	if _, err := Parse(withoutField); err == nil {
		t.Fatal("missing required field accepted")
	}
	missing["fault_code"] = nil
	withNull, _ := json.Marshal(missing)
	if _, err := Parse(withNull); err == nil {
		t.Fatal("null required field accepted")
	}
}
