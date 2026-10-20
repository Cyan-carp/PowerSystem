package api

import (
	"encoding/json"
	"testing"
	"time"
)

func TestHealthSampleAndReceptionFreshness(t *testing.T) {
	now := time.Date(2026, 10, 19, 5, 0, 0, 0, time.UTC)
	for _, tc := range []struct {
		name     string
		sampled  any
		received any
		status   any
		level    string
		state    string
		score    float64 // -1 means unknown
	}{
		{"fresh", now.UnixMilli(), now, 1, "", "online", 100},
		{"stopped", now.UnixMilli(), now, 0, "", "online", 100},
		{"fault", now.UnixMilli(), now, 2, "", "fault", 0},
		{"urgent", now.UnixMilli(), now, 1, "urgent", "online", 40},
		{"major", now.UnixMilli(), now, 1, "major", "online", 70},
		{"minor", now.UnixMilli(), now, 1, "minor", "online", 90},
		{"freshness_boundary", now.Add(-15 * time.Second).UnixMilli(), now.Add(-15 * time.Second), 1, "", "online", 100},
		{"just_received_old_sample", now.Add(-time.Hour).UnixMilli(), now, 1, "", "unknown", -1},
		{"stale_fault_sample", now.Add(-16 * time.Second).UnixMilli(), now, 2, "", "unknown", -1},
		{"valid_old_reception", now.Add(-time.Hour).UnixMilli(), now.Add(-time.Hour), 1, "", "offline", 0},
		{"reception_expired", now.Add(-16 * time.Second).UnixMilli(), now.Add(-16 * time.Second), 1, "", "offline", 0},
		{"future_reception", now.UnixMilli(), now.Add(time.Second), 1, "", "unknown", -1},
		{"zero_reception", now.UnixMilli(), time.Time{}, 1, "", "unknown", -1},
		{"null_reception", now.UnixMilli(), nil, 1, "", "unknown", -1},
		{"null_sample", nil, now, 1, "", "unknown", -1},
		{"zero_sample", int64(0), now, 1, "", "unknown", -1},
		{"future_sample", now.Add(61 * time.Second).UnixMilli(), now, 1, "", "unknown", -1},
		{"sample_clock_tolerance", now.Add(60 * time.Second).UnixMilli(), now, 1, "", "online", 100},
		{"sample_after_old_reception", now.UnixMilli(), now.Add(-time.Hour), 1, "", "unknown", -1},
		{"null_status", now.UnixMilli(), now, nil, "", "unknown", -1},
	} {
		t.Run(tc.name, func(t *testing.T) {
			raw, err := json.Marshal(map[string]any{"ts_ms": tc.sampled, "received_at": tc.received, "status": tc.status})
			if err != nil {
				t.Fatal(err)
			}
			got := scoreLatestHealth(deviceHealth{State: "unknown"}, raw, tc.level, now)
			if got.State != tc.state {
				t.Fatalf("state=%s, want %s", got.State, tc.state)
			}
			if tc.score < 0 {
				if got.Score != nil {
					t.Fatalf("score=%v, want unknown", *got.Score)
				}
			} else if got.Score == nil || *got.Score != tc.score {
				t.Fatalf("score=%v, want %v", got.Score, tc.score)
			}
		})
	}
	for _, raw := range []string{`{}`, `{"received_at":"2026-10-19T05:00:00Z","status":1}`, `{"ts_ms":1792386000000,"status":1}`, `not-json`} {
		got := scoreLatestHealth(deviceHealth{State: "unknown"}, []byte(raw), "", now)
		if got.Score != nil || got.State != "unknown" {
			t.Fatalf("incomplete sample %q was scored", raw)
		}
	}
}
