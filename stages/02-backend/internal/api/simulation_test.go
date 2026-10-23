package api

import (
	"context"
	"testing"
	"time"

	"powersystem/backend/internal/config"
	"powersystem/backend/internal/service"
)

func TestNormalizeSimulationAlarmMode(t *testing.T) {
	for _, tc := range []struct{ raw, want string }{
		{"", service.SimulationModeOff},
		{"off", service.SimulationModeOff},
		{"controlled", service.SimulationModePaused},
		{"paused", service.SimulationModePaused},
		{"CONTROLLED", service.SimulationModeOff},
		{"unknown", service.SimulationModeOff},
	} {
		if got := normalizeSimulationAlarmMode(tc.raw); got != tc.want {
			t.Fatalf("normalizeSimulationAlarmMode(%q) = %q, want %q", tc.raw, got, tc.want)
		}
	}
}

func TestSimulationAlarmWorkerStopsWhenOff(t *testing.T) {
	s := &Server{cfg: config.Config{SimulationAlarmMode: "off"}}
	done := make(chan struct{})
	go func() {
		s.simulationAlarmWorker(context.Background())
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("worker did not return in off mode")
	}
}
