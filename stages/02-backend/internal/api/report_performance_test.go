package api

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"powersystem/backend/internal/config"
	"powersystem/backend/internal/model"
	"powersystem/backend/internal/tdengine"
)

func TestAvailabilityStreamingAcrossPages(t *testing.T) {
	start := int64(1792285200000)
	points := []samplePoint{{TS: start - 7000, Status: 2}}
	// More than two pages, with both a short cross-page interval and a long gap.
	for i := 0; i < 10003; i++ {
		ts := start + int64(i)*1000
		if i >= 5001 {
			ts += 30000
		}
		points = append(points, samplePoint{TS: ts, Status: i % 3})
	}
	end := points[len(points)-1].TS + 20000
	var requests atomic.Int32
	fake := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		q := string(raw)
		rows := [][]any{}
		if strings.HasPrefix(q, "SELECT ts,status") {
			if strings.Contains(q, "WHERE ts <=") {
				rows = append(rows, []any{points[0].TS, points[0].Status})
			} else {
				requests.Add(1)
				if strings.Contains(q, "OFFSET") {
					t.Error("availability used OFFSET")
				}
				var id, after, until int64
				var limit int
				if _, err := fmt.Sscanf(q, "SELECT ts,status FROM test.t_device_%d WHERE ts > %d AND ts < %d ORDER BY ts ASC LIMIT %d", &id, &after, &until, &limit); err != nil {
					t.Error(err)
				}
				for _, p := range points[1:] {
					if p.TS > after && p.TS < until && len(rows) < limit {
						rows = append(rows, []any{p.TS, p.Status})
					}
				}
			}
		}
		json.NewEncoder(w).Encode(map[string]any{"code": 0, "data": rows})
	}))
	defer fake.Close()
	s := &Server{td: tdengine.New(config.Config{TDURL: fake.URL, TDDatabase: "test"})}
	got, err := s.loadAvailability(context.Background(), model.Device{ID: 1, DeviceCode: "TEST", StationCode: "ST"}, start, end)
	if err != nil {
		t.Fatal(err)
	}
	want := computeAvailability(points, start, end)
	if !reflect.DeepEqual(got, want) || requests.Load() != 3 {
		t.Fatalf("streaming mismatch/pages=%d: got %+v want %+v", requests.Load(), got, want)
	}
	// 30-second gap beyond 15 seconds plus the final five seconds are unknown.
	if got.UnknownMs != 21000 {
		t.Fatalf("gap semantics changed: %+v", got)
	}
}

func TestAvailabilityConcurrencyOrderAndCancellation(t *testing.T) {
	var active, peak atomic.Int32
	fake := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		n := active.Add(1)
		defer active.Add(-1)
		for {
			old := peak.Load()
			if n <= old || peak.CompareAndSwap(old, n) {
				break
			}
		}
		select {
		case <-r.Context().Done():
			return
		case <-time.After(15 * time.Millisecond):
		}
		json.NewEncoder(w).Encode(map[string]any{"code": 0, "data": [][]any{}})
	}))
	defer fake.Close()
	s := &Server{td: tdengine.New(config.Config{TDURL: fake.URL, TDDatabase: "test"})}
	devices := make([]model.Device, 8)
	for i := range devices {
		devices[i] = model.Device{ID: int64(i + 1), DeviceCode: fmt.Sprint("D", i), StationCode: "ST"}
	}
	end := time.Now().UTC()
	got, err := s.availabilityDevices(context.Background(), devices, end.Add(-time.Hour), end)
	if err != nil {
		t.Fatal(err)
	}
	if peak.Load() < 2 || peak.Load() > 3 {
		t.Fatalf("unexpected concurrency %d", peak.Load())
	}
	for i, res := range got {
		if res.DeviceID != devices[i].ID || res.WindowMs != 3600000 || !res.Insufficient {
			t.Fatalf("order/result: %+v", got)
		}
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if rows, err := s.availabilityDevices(ctx, devices, end.Add(-time.Hour), end); err == nil || rows != nil {
		t.Fatal("cancelled report returned partial success")
	}
}

func TestAvailabilityRejectsNonAdvancingCursor(t *testing.T) {
	start := int64(1792285200000)
	for _, rows := range [][][]any{
		{{start, 1}, {start, 2}}, {{start + 1, 1}, {start, 2}}, {{start - 1, 1}}, {{start + 15000, 1}},
	} {
		source := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			raw, _ := io.ReadAll(r.Body)
			data := [][]any{}
			if strings.Contains(string(raw), "WHERE ts >") {
				data = rows
			}
			json.NewEncoder(w).Encode(map[string]any{"code": 0, "data": data})
		}))
		s := &Server{td: tdengine.New(config.Config{TDURL: source.URL, TDDatabase: "test"})}
		if _, err := s.loadAvailability(context.Background(), model.Device{ID: 1, DeviceCode: "D", StationCode: "ST"}, start, start+15000); err == nil {
			t.Fatalf("bad cursor accepted: %v", rows)
		}
		source.Close()
	}
}

func TestAvailabilityReadFailureCancelsSiblingDevices(t *testing.T) {
	var arrived atomic.Int32
	ready := make(chan struct{})
	cancelled := make(chan struct{}, 2)
	fake := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		query := string(raw)
		if strings.Contains(query, "WHERE ts <=") {
			if arrived.Add(1) == 3 {
				close(ready)
			}
			select {
			case <-ready:
			case <-r.Context().Done():
				return
			}
			if strings.Contains(query, "t_device_2 ") {
				json.NewEncoder(w).Encode(map[string]any{"code": 1, "desc": "read failed"})
				return
			}
			<-r.Context().Done()
			cancelled <- struct{}{}
			return
		}
		json.NewEncoder(w).Encode(map[string]any{"code": 0, "data": [][]any{}})
	}))
	defer fake.Close()
	devices := []model.Device{{ID: 1, DeviceCode: "D1", StationCode: "ST"}, {ID: 2, DeviceCode: "D2", StationCode: "ST"}, {ID: 3, DeviceCode: "D3", StationCode: "ST"}}
	s := &Server{td: tdengine.New(config.Config{TDURL: fake.URL, TDDatabase: "test"})}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	end := time.Now().UTC()
	if rows, err := s.availabilityDevices(ctx, devices, end.Add(-time.Hour), end); err == nil || rows != nil {
		t.Fatal("failed read produced partial report")
	}
	if ctx.Err() != nil {
		t.Fatal("siblings waited for parent timeout rather than cancelling on failure")
	}
	for i := 0; i < 2; i++ {
		select {
		case <-cancelled:
		case <-ctx.Done():
			t.Fatal("sibling HTTP request not cancelled")
		}
	}
}
