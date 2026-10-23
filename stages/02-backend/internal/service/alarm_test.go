package service

import (
	"testing"
	"time"
)

func TestMatchesAtThreshold(t *testing.T) {
	for _, tc := range []struct {
		op   string
		want bool
	}{
		{">", false}, {">=", true}, {"<", false}, {"<=", true}, {"invalid", false},
	} {
		if got := Matches(tc.op, 80, 80); got != tc.want {
			t.Errorf("Matches(%q,80,80)=%v; want %v", tc.op, got, tc.want)
		}
	}
}

func TestFleetSlotWindow(t *testing.T) {
	for _, tc := range []struct {
		station string
		metric  string
		hour    int
	}{
		{"PV-01", "temperature", 9},
		{"PV-02", "voltage", 11},
		{"PV-03", "temperature", 13},
		{"PV-04", "current", 15},
		{"ST-01", "power", 17},
	} {
		start := time.Date(2026, 10, 22, tc.hour, 0, 0, 0, SimulationLocation())
		if !fleetSlotWindow(tc.station, tc.metric, start) || !fleetSlotWindow(tc.station, tc.metric, start.Add(9*time.Minute+59*time.Second)) {
			t.Fatalf("slot not open for %s/%s", tc.station, tc.metric)
		}
		if fleetSlotWindow(tc.station, tc.metric, start.Add(10*time.Minute)) || fleetSlotWindow(tc.station, "other", start) {
			t.Fatalf("slot leaked for %s/%s", tc.station, tc.metric)
		}
	}
	if fleetSlotWindow("REAL", "temperature", time.Date(2026, 10, 22, 9, 0, 0, 0, SimulationLocation())) {
		t.Fatal("physical station entered synthetic slot")
	}
}
