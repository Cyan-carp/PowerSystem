package api

import (
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
)

func TestTDMissingMetricsExposeFixedCountersAndState(t *testing.T) {
	gin.SetMode(gin.TestMode)
	s := &Server{}
	s.telemetry.counts = map[string]uint64{
		"td_query_hit":                 2,
		"td_query_missing":             5,
		"td_query_error":               1,
		"td_missing_mature":            3,
		"td_missing_circuit_open":      1,
		"td_missing_circuit_skip":      4,
		"td_missing_circuit_half_open": 1,
		"td_missing_circuit_recovered": 1,
	}
	s.telemetry.times = map[string]time.Time{"td_query_missing": time.Now()}
	w := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(w)
	c.Request = httptest.NewRequest("GET", "/metrics", nil)
	s.serveTelemetryMetrics(c)
	body := w.Body.String()
	for _, want := range []string{
		"powersystem_telemetry_td_query_hit_total 2",
		"powersystem_telemetry_td_query_missing_total 5",
		"powersystem_telemetry_td_query_error_total 1",
		"powersystem_telemetry_td_missing_mature_total 3",
		"powersystem_telemetry_td_missing_circuit_open_total 1",
		"powersystem_telemetry_td_missing_circuit_skip_total 4",
		"powersystem_telemetry_td_missing_circuit_half_open_total 1",
		"powersystem_telemetry_td_missing_circuit_recovered_total 1",
		"powersystem_telemetry_td_missing_circuit_state 0",
	} {
		if !strings.Contains(body, want) {
			t.Fatalf("missing metric %q in response:\n%s", want, body)
		}
	}
}

func TestTDMissingMetricsStateTracksCooldown(t *testing.T) {
	gin.SetMode(gin.TestMode)
	s := &Server{}
	s.tdMissingCircuit.enabled = true
	s.tdMissingCircuit.state = tdMissingStateOpen
	s.tdMissingCircuit.until = time.Now().Add(time.Minute)
	read := func() string {
		w := httptest.NewRecorder()
		c, _ := gin.CreateTestContext(w)
		c.Request = httptest.NewRequest("GET", "/metrics", nil)
		s.serveTelemetryMetrics(c)
		return w.Body.String()
	}
	if body := read(); !strings.Contains(body, "powersystem_telemetry_td_missing_circuit_state 1\n") {
		t.Fatalf("open state absent: %s", body)
	}
	s.tdMissingCircuit.mu.Lock()
	s.tdMissingCircuit.until = time.Now().Add(-time.Second)
	s.tdMissingCircuit.mu.Unlock()
	if body := read(); !strings.Contains(body, "powersystem_telemetry_td_missing_circuit_state 2\n") {
		t.Fatalf("half-open eligible state absent: %s", body)
	}
}
