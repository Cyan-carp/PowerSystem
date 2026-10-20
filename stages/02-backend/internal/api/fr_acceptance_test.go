package api

import (
	"encoding/csv"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"regexp"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"go.uber.org/zap"
	"powersystem/backend/internal/config"
	"powersystem/backend/internal/model"
	"powersystem/backend/internal/tdengine"
)

func TestFR10TwoStationsRecomputeAndCompetitionRank(t *testing.T) {
	score := func(v float64) *float64 { return &v }
	items := rankScores([]deviceHealth{
		{DeviceID: 90, DeviceCode: "A-01", StationCode: "A", Score: score(100)},
		{DeviceID: 1, DeviceCode: "A-02", StationCode: "A", Score: score(40)},
		{DeviceID: 2, DeviceCode: "A-03", StationCode: "A"},
		{DeviceID: 3, DeviceCode: "B-01", StationCode: "B", Score: score(100)},
		{DeviceID: 4, DeviceCode: "B-02", StationCode: "B", Score: score(70)},
		{DeviceID: 5, DeviceCode: "B-03", StationCode: "B", Score: score(90)},
	})
	if items[0].DeviceCode != "A-01" || items[1].DeviceCode != "B-01" || *items[0].Rank != 1 || *items[1].Rank != 1 || *items[2].Rank != 3 || items[5].Rank != nil {
		t.Fatalf("ranks=%+v", items)
	}
	stations := aggregateHealth(items)
	if len(stations) != 2 || stations[0].StationCode != "B" || stations[0].Score == nil || *stations[0].Score != 260.0/3 || *stations[0].Rank != 1 {
		t.Fatalf("formal station: %+v", stations)
	}
	if stations[1].StationCode != "A" || *stations[1].Score != 70 || !stations[1].Provisional || stations[1].Rank != nil || stations[1].UnknownCount != 1 {
		t.Fatalf("provisional station: %+v", stations[1])
	}
	avg, count := healthAverage(items)
	if count != 5 || *avg != 80 || allHealthKnown(items) {
		t.Fatalf("overview avg=%v valid=%d", avg, count)
	}
	// Keep small but real score differences until display formatting.
	near := aggregateHealth([]deviceHealth{{StationCode: "A", Score: score(70.000001)}, {StationCode: "B", Score: score(70.000002)}})
	if near[0].StationCode != "B" || *near[1].Rank != 2 {
		t.Fatal("station scores rounded before ranking")
	}
}

func TestFR12SeparateLifecycleCohortsAndDurationAggregation(t *testing.T) {
	start := time.Date(2026, 10, 19, 0, 0, 0, 0, time.UTC)
	end := start.Add(time.Hour)
	at := func(n int) *time.Time { v := start.Add(time.Duration(n) * time.Second); return &v }
	reason := func(s string) *string { return &s }
	res := summarizeTimeliness([]model.AlarmRecord{
		{TriggeredAt: start, AckedAt: at(10), RecoveredAt: at(30), CloseReason: reason("telemetry_recovered")},
		{TriggeredAt: start, RecoveredAt: at(90), CloseReason: reason("prediction_risk_resolved")},
		{TriggeredAt: start, RecoveredAt: at(100), CloseReason: reason("rule_disabled")},
		{TriggeredAt: start, RecoveredAt: at(110), CloseReason: reason("unknown")},
		{TriggeredAt: start, AckedAt: at(3601), RecoveredAt: at(3602)},
	}, end)
	if res.EventCount != 5 || res.AckTimeliness.Count != 1 || res.PendingAck != 4 || res.PendingRecover != 1 || res.ManagedRecover != 1 || res.UnknownRecover != 1 || res.TelemetryRecoverTimeliness.Count != 1 || *res.TelemetryRecoverTimeliness.MeanMs != 30000 || *res.PredictionRecoverTimeliness.MeanMs != 90000 {
		t.Fatalf("cohorts=%+v", res)
	}
	a := computeAvailability([]samplePoint{{TS: 0, Status: 0}, {TS: 10000, Status: 2}}, 0, 30000)
	b := computeAvailability([]samplePoint{{TS: 0, Status: 1}}, 0, 10000)
	a.StationCode = "A"
	b.StationCode = "A"
	stations := (&Server{}).aggregateAvailability([]availabilityResult{a, b})
	st := stations[0]
	if st.WindowMs != 40000 || st.CoveredMs != 35000 || st.UnknownMs != 5000 || *st.ObservedAvail != 20000.0/35000 || *st.DataCoverage != 35000.0/40000 {
		t.Fatalf("duration-weighted aggregate=%+v", st)
	}
}

func TestFR11PagedCSVMatchesSourceAndRejectsConcurrentOverflow(t *testing.T) {
	gin.SetMode(gin.TestMode)
	for _, tc := range []struct {
		name                          string
		count, initialCount, wantCode int
	}{{"complete", 10001, 10001, 200}, {"empty", 0, 0, 200}, {"concurrent_backfill", 200001, 200000, 400}, {"changed_count", 10, 9, 409}} {
		t.Run(tc.name, func(t *testing.T) {
			const start = int64(1792281600000)
			cursor := regexp.MustCompile(`AND \(ts > ([0-9]+) OR`)
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				raw, _ := io.ReadAll(r.Body)
				query := string(raw)
				rows := [][]any{}
				if strings.HasPrefix(query, "SELECT COUNT") {
					rows = [][]any{{tc.initialCount}}
				} else if strings.HasPrefix(query, "SELECT ts,seq") {
					offset := 0
					if m := cursor.FindStringSubmatch(query); len(m) > 0 {
						ts, _ := strconv.ParseInt(m[1], 10, 64)
						offset = int(ts-start) + 1
					}
					for i := offset; i < offset+exportPageSize && i < tc.count; i++ {
						rows = append(rows, []any{time.UnixMilli(start + int64(i)).UTC().Format(time.RFC3339Nano), i, 400.93, 100, 30, 68, 1, 0})
					}
				}
				json.NewEncoder(w).Encode(map[string]any{"code": 0, "data": rows})
			}))
			defer server.Close()
			s := &Server{db: reportRegressionDB(t), td: tdengine.New(config.Config{TDURL: server.URL, TDDatabase: "fixture"})}
			w := httptest.NewRecorder()
			c, _ := gin.CreateTestContext(w)
			c.Params = gin.Params{{Key: "id", Value: "1"}}
			c.Request = httptest.NewRequest("GET", "/export?start=2026-10-18T00:00:00Z&end=2026-10-18T02:00:00Z", nil)
			s.exportTelemetry(c)
			if w.Code != tc.wantCode {
				t.Fatalf("HTTP=%d want=%d body=%s", w.Code, tc.wantCode, w.Body.String()[:min(200, w.Body.Len())])
			}
			if tc.wantCode != 200 {
				if w.Header().Get("Content-Disposition") != "" {
					t.Fatal("failure returned a download")
				}
				return
			}
			records, err := csv.NewReader(strings.NewReader(strings.TrimPrefix(w.Body.String(), "\xef\xbb\xbf"))).ReadAll()
			if err != nil {
				t.Fatal(err)
			}
			if len(records) != tc.count+1 || w.Header().Get("X-Row-Count") != strconv.Itoa(tc.count) {
				t.Fatal("export row count differs from source")
			}
			if tc.count > 0 && (records[1][2] != fmt.Sprint(start) || records[tc.count][2] != fmt.Sprint(start+int64(tc.count-1)) || records[tc.count][4] != fmt.Sprint(tc.count-1)) {
				t.Fatal("first/last records differ from source")
			}
		})
	}
}

func TestFR12CSVUsesSameComputedResults(t *testing.T) {
	start := time.Date(2026, 10, 19, 0, 0, 0, 0, time.UTC)
	end := start.Add(time.Minute)
	d := computeAvailability([]samplePoint{{TS: start.UnixMilli(), Status: 1}}, start.UnixMilli(), end.UnixMilli())
	d.DeviceCode = "=device"
	d.StationCode = "ST"
	s := &Server{}
	stations := s.aggregateAvailability([]availabilityResult{d})
	w := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(w)
	s.availabilityCSV(c, []availabilityResult{d}, stations, start, end, "computed")
	rows, err := csv.NewReader(strings.NewReader(strings.TrimPrefix(w.Body.String(), "\xef\xbb\xbf"))).ReadAll()
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 3 || rows[1][1] != "'=device" || rows[1][8] != reportInt(d.CoveredMs) || rows[1][9] != reportNumber(d.ObservedAvail) || rows[1][10] != reportNumber(d.DataCoverage) || rows[2][8] != reportInt(stations[0].CoveredMs) {
		t.Fatalf("CSV differs from computation: %v", rows)
	}
}

func TestFRReadAndDownloadRoutesRequireAuthentication(t *testing.T) {
	s := New(config.Config{JWTSecret: strings.Repeat("x", 32)}, nil, nil, nil, zap.NewNop())
	for _, path := range []string{"/api/v1/health/devices", "/api/v1/health/stations", "/api/v1/reports/availability?format=csv", "/api/v1/reports/alarm-timeliness?format=csv", "/api/v1/devices/1/telemetry/export"} {
		w := httptest.NewRecorder()
		s.Handler().ServeHTTP(w, httptest.NewRequest("GET", path, nil))
		if w.Code != 401 {
			t.Fatalf("%s HTTP=%d", path, w.Code)
		}
	}
}
