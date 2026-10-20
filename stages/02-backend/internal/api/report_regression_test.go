package api

import (
	"context"
	"database/sql"
	"encoding/csv"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	_ "modernc.org/sqlite"

	"powersystem/backend/internal/config"
	"powersystem/backend/internal/model"
	"powersystem/backend/internal/tdengine"
)

// Exercise handler queries in memory, with no service credentials or external DB.
// This verifies Go result handling; PostgreSQL compatibility has separate integration tests.
func reportRegressionDB(t *testing.T) *gorm.DB {
	t.Helper()
	conn, err := sql.Open("sqlite", ":memory:")
	if err != nil {
		t.Fatal(err)
	}
	conn.SetMaxOpenConns(1)
	t.Cleanup(func() { conn.Close() })
	db, err := gorm.Open(postgres.New(postgres.Config{Conn: conn}), &gorm.Config{})
	if err != nil {
		t.Fatal(err)
	}
	for _, query := range []string{
		`CREATE TABLE devices(id INTEGER PRIMARY KEY,device_code TEXT,station_code TEXT,deleted_at timestamp)`,
		`CREATE TABLE alarm_records(id INTEGER PRIMARY KEY,device_id INTEGER,triggered_at timestamp,acked_at timestamp,recovered_at timestamp,close_reason TEXT)`,
		`INSERT INTO devices VALUES(1,'TEST','ST',NULL)`,
	} {
		if err := db.Exec(query).Error; err != nil {
			t.Fatal(err)
		}
	}
	return db
}

func reportRegressionTD(t *testing.T, boundary, rows [][]any) *tdengine.Client {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		query := string(body)
		data := [][]any{}
		switch {
		case strings.HasPrefix(query, "SELECT COUNT"):
			data = [][]any{{len(rows)}}
		case strings.HasPrefix(query, "SELECT ts,status"):
			if strings.Contains(query, "WHERE ts <=") {
				data = boundary
			} else {
				for _, row := range rows {
					if len(row) > 6 {
						data = append(data, []any{row[0], row[6]})
					} else {
						data = append(data, row)
					}
				}
			}
		case strings.HasPrefix(query, "SELECT ts,seq"):
			data = rows
		}
		json.NewEncoder(w).Encode(map[string]any{"code": 0, "data": data})
	}))
	t.Cleanup(server.Close)
	return tdengine.New(config.Config{TDURL: server.URL, TDDatabase: "regression"})
}

func TestAvailabilityTDTimeFormatsAndBoundary(t *testing.T) {
	start := time.Date(2026, 10, 18, 1, 0, 0, 123000000, time.UTC)
	for _, stamp := range []any{start.Format(time.RFC3339Nano), start.In(time.FixedZone("CST", 8*3600)).Format(time.RFC3339Nano), start.UnixMilli(), strconv.FormatInt(start.UnixMilli(), 10)} {
		t.Run(fmt.Sprint(stamp), func(t *testing.T) {
			s := &Server{td: reportRegressionTD(t, nil, [][]any{{stamp, 1, 400, 100, 30, 68, 1, 0}})}
			got, err := s.loadAvailability(context.Background(), model.Device{ID: 1, DeviceCode: "TEST", StationCode: "ST"}, start.UnixMilli(), start.Add(15*time.Second).UnixMilli())
			if err != nil {
				t.Fatal(err)
			}
			if got.CoveredMs != 15000 || got.RunningMs != 15000 || got.ObservedAvail == nil || *got.ObservedAvail != 1 {
				t.Fatalf("valid sample lost: %+v", got)
			}
		})
	}
	boundary := [][]any{{start.Add(-10 * time.Second).Format(time.RFC3339Nano), 1}}
	rows := [][]any{{start.Add(5 * time.Second).Format(time.RFC3339Nano), 2, 400, 100, 30, 68, 2, 0}}
	s := &Server{td: reportRegressionTD(t, boundary, rows)}
	got, err := s.loadAvailability(context.Background(), model.Device{ID: 1, DeviceCode: "TEST", StationCode: "ST"}, start.UnixMilli(), start.Add(10*time.Second).UnixMilli())
	if err != nil {
		t.Fatal(err)
	}
	if got.RunningMs != 5000 || got.FaultMs != 5000 || got.CoveredMs != 10000 {
		t.Fatalf("boundary sample not applied: %+v", got)
	}
}

func TestAvailabilityMalformedRowsFailInsteadOfBecomingGaps(t *testing.T) {
	for _, tc := range []struct {
		name           string
		boundary, rows [][]any
	}{
		{"boundary_timestamp", [][]any{{"bad-time", 1}}, nil},
		{"sample_timestamp", nil, [][]any{{"bad-time", 1, 400, 100, 30, 68, 1, 0}}},
		{"short_row", nil, [][]any{{"2026-10-18T01:00:00Z"}}},
		{"missing_status", nil, [][]any{{"2026-10-18T01:00:00Z", 1, 400, 100, 30, 68, nil, 0}}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			s := &Server{td: reportRegressionTD(t, tc.boundary, tc.rows)}
			if _, err := s.loadAvailability(context.Background(), model.Device{ID: 1, DeviceCode: "TEST", StationCode: "ST"}, 1792285200000, 1792285215000); err == nil {
				t.Fatal("malformed row silently accepted")
			}
		})
	}
}

func TestExportTDTimeColumnsAndInvalidTimestamp(t *testing.T) {
	gin.SetMode(gin.TestMode)
	stamp := time.Date(2026, 10, 18, 1, 0, 0, 123000000, time.UTC)
	for _, tc := range []struct {
		name     string
		stamp    any
		wantCode int
	}{
		{"rfc3339", stamp.Format(time.RFC3339Nano), 200},
		{"offset", stamp.In(time.FixedZone("CST", 8*3600)).Format(time.RFC3339Nano), 200},
		{"milliseconds", stamp.UnixMilli(), 200},
		{"invalid", "bad-time", 503},
		{"missing", nil, 503},
	} {
		t.Run(tc.name, func(t *testing.T) {
			s := &Server{db: reportRegressionDB(t), td: reportRegressionTD(t, nil, [][]any{{tc.stamp, 1, 400, 100, 30, 68, 1, 0}})}
			w := httptest.NewRecorder()
			c, _ := gin.CreateTestContext(w)
			c.Params = gin.Params{{Key: "id", Value: "1"}}
			c.Request = httptest.NewRequest("GET", "/export?start=2026-10-18T00:00:00Z&end=2026-10-18T02:00:00Z", nil)
			s.exportTelemetry(c)
			if w.Code != tc.wantCode {
				t.Fatalf("HTTP %d, want %d: %s", w.Code, tc.wantCode, w.Body.String())
			}
			if tc.wantCode != 200 {
				if w.Header().Get("Content-Disposition") != "" || strings.HasPrefix(w.Body.String(), "\xef\xbb\xbf") {
					t.Fatal("invalid data returned as a successful file")
				}
				return
			}
			records, err := csv.NewReader(strings.NewReader(strings.TrimPrefix(w.Body.String(), "\xef\xbb\xbf"))).ReadAll()
			if err != nil {
				t.Fatal(err)
			}
			if len(records) != 2 || records[1][2] != strconv.FormatInt(stamp.UnixMilli(), 10) || records[1][3] != "2026-10-18 09:00:00.123" {
				t.Fatalf("wrong timestamp columns: %v", records)
			}
		})
	}
}

func TestAlarmTimelinessAsOfEnd(t *testing.T) {
	gin.SetMode(gin.TestMode)
	end := time.Now().UTC().Add(-time.Hour).Truncate(time.Second)
	trigger := end.Add(-30 * time.Minute)
	before, after := end.Add(-10*time.Minute), end.Add(10*time.Minute)
	for _, tc := range []struct {
		name                                               string
		acked, recovered                                   *time.Time
		ackCount, pendingAck, recoverCount, pendingRecover int
	}{
		{"ack_before_end", &before, nil, 1, 0, 0, 1},
		{"ack_at_end", &end, nil, 1, 0, 0, 1},
		{"ack_after_end", &after, nil, 0, 1, 0, 1},
		{"ack_after_end_already_recovered", &after, &before, 0, 1, 1, 0},
		{"recovered_without_ack", nil, &before, 0, 1, 1, 0},
		{"neither_complete", nil, nil, 0, 1, 0, 1},
		{"recover_after_end", &before, &after, 1, 0, 0, 1},
	} {
		t.Run(tc.name, func(t *testing.T) {
			db := reportRegressionDB(t)
			if err := db.Exec("INSERT INTO alarm_records VALUES(1,1,?,?,?,'telemetry_recovered')", trigger, tc.acked, tc.recovered).Error; err != nil {
				t.Fatal(err)
			}
			w := httptest.NewRecorder()
			c, _ := gin.CreateTestContext(w)
			params := url.Values{"start": {end.Add(-time.Hour).Format(time.RFC3339)}, "end": {end.Format(time.RFC3339)}}
			c.Request = httptest.NewRequest("GET", "/timeliness?"+params.Encode(), nil)
			(&Server{db: db}).alarmTimeliness(c)
			if w.Code != 200 {
				t.Fatalf("HTTP %d: %s", w.Code, w.Body.String())
			}
			var response struct {
				Data struct {
					Result timelinessResult `json:"result"`
				} `json:"data"`
			}
			if err := json.Unmarshal(w.Body.Bytes(), &response); err != nil {
				t.Fatal(err)
			}
			got := response.Data.Result
			if got.AckTimeliness.Count != tc.ackCount || got.PendingAck != tc.pendingAck || got.RecoverTimeliness.Count != tc.recoverCount || got.PendingRecover != tc.pendingRecover {
				t.Fatalf("incorrect as-of-end classification: %+v", got)
			}
			if got.AckTimeliness.Count+got.PendingAck != 1 || got.RecoverTimeliness.Count+got.PendingRecover+got.ManagedOrUnknownRecover != 1 {
				t.Fatal("event disappeared from cohort")
			}
			if tc.ackCount == 1 && (got.AckTimeliness.MeanMs == nil || *got.AckTimeliness.MeanMs != float64(tc.acked.Sub(trigger).Milliseconds())) {
				t.Fatalf("incorrect confirmation duration: %+v", got.AckTimeliness)
			}
		})
	}
}
