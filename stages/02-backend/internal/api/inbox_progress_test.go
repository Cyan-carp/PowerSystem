package api

import (
	"go.uber.org/zap"
	"go.uber.org/zap/zaptest/observer"
	"math"
	"testing"
	"time"
)

func TestTelemetryUnknownAndClockBoundaries(t *testing.T) {
	now := time.Now()
	future := now.Add(time.Second)
	for _, at := range []*time.Time{nil, {}, &future} {
		if !math.IsNaN(telemetryAge(at, now)) {
			t.Fatal("unknown/future time must not be healthy zero")
		}
	}
	old := now.Add(-time.Hour)
	if telemetryAge(&old, now) != 3600 {
		t.Fatal("age changed")
	}
	if !math.IsNaN(unknownTelemetryValues()["fresh_ratio"]) {
		t.Fatal("uninitialized coverage must be unknown")
	}
}

func TestTelemetryLogUsesElapsedTime(t *testing.T) {
	core, entries := observer.New(zap.WarnLevel)
	s := &Server{log: zap.New(core)}
	start := time.Now()
	for i := 0; i < 1000; i++ {
		s.telemetryLog("td_unavailable", start.Add(time.Duration(i)*time.Millisecond))
	}
	s.telemetryLog("td_unavailable", start.Add(59*time.Second))
	if entries.Len() != 1 {
		t.Fatal("log rate followed rounds instead of elapsed time")
	}
	s.telemetryLog("td_unavailable", start.Add(time.Minute))
	if entries.Len() != 2 {
		t.Fatal("elapsed minute did not permit next log")
	}
}
