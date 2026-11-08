package api

import (
	"encoding/json"
	"math"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
)

func TestDemoConsoleInboxOperationsUsesBoundedSnapshot(t *testing.T) {
	gin.SetMode(gin.TestMode)
	s := &Server{expiry: inboxExpirySettings{Enabled: true}}
	s.telemetry.auxValues = unknownInboxQueueValues()
	s.telemetry.counts = map[string]uint64{"expiry_rows": 12, "expiry_probe_rows": 2}
	call := func() map[string]any {
		t.Helper()
		w := httptest.NewRecorder()
		c, _ := gin.CreateTestContext(w)
		c.Request = httptest.NewRequest("GET", "/api/v1/demo-console/inbox-operations", nil)
		s.demoConsoleInboxOperations(c)
		var body struct {
			Data map[string]any `json:"data"`
		}
		if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil || w.Code != 200 {
			t.Fatalf("response=%d %s err=%v", w.Code, w.Body.String(), err)
		}
		return body.Data
	}
	unknown := call()
	if unknown["known"] != false || unknown["pending_in_window"] != nil || unknown["pending_outside_window"] != nil {
		t.Fatalf("unknown queue must remain unknown: %+v", unknown)
	}
	s.telemetry.auxValues = map[string]float64{"inbox_queue_stats_known": 1, "inbox_pending_rows_in_window": 7, "inbox_pending_rows_outside_window": 3}
	s.telemetry.times = map[string]time.Time{"inbox_queue": time.Now().UTC(), "expiry_run": time.Now().UTC()}
	known := call()
	if known["known"] != true || known["pending_in_window"] != float64(7) || known["pending_outside_window"] != float64(3) || known["expiry_rows_since_start"] != float64(10) || known["s02_enabled"] != true {
		t.Fatalf("wrong bounded snapshot: %+v", known)
	}
	s.telemetry.auxValues["inbox_pending_rows_in_window"] = math.NaN()
	if got := call(); got["known"] != false || got["pending_in_window"] != nil {
		t.Fatalf("NaN must not become a displayed count: %+v", got)
	}
}
