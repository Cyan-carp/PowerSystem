package api

import (
	"encoding/json"
	"net/http/httptest"
	"os"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
)

func TestStatisticsValidation(t *testing.T) {
	gin.SetMode(gin.TestMode)
	now := time.Date(2026, 10, 9, 12, 0, 0, 0, time.UTC)
	for _, tc := range []struct {
		query string
		valid bool
	}{
		{"", true}, {"?group_by=level", true}, {"?group_by=status&device_id=2", true},
		{"?group_by=metric", true}, {"?group_by=device%3BDROP%20TABLE%20devices", false},
		{"?start=2026-10-01", false}, {"?start=2026-10-10T00:00:00Z", false},
		{"?start=2020-01-01T00:00:00Z", false}, {"?end=2026-10-10T00:00:00Z", false},
		{"?status=all", false}, {"?device_id=0", false}, {"?sql=SELECT", false},
		{"?group_by=device&group_by=status", false},
	} {
		c, _ := gin.CreateTestContext(httptest.NewRecorder())
		c.Request = httptest.NewRequest("GET", "/statistics"+tc.query, nil)
		q, valid := parseAlarmStatistics(c, now)
		if valid != tc.valid {
			t.Fatalf("%q: valid=%v", tc.query, valid)
		}
		if tc.query == "" && (q.End != now || !q.Start.Equal(now.AddDate(0, 0, -30))) {
			t.Fatal("default is not 30 days")
		}
	}
	s := NewForStatisticsAuthTest()
	w := httptest.NewRecorder()
	s.ServeHTTP(w, httptest.NewRequest("GET", "/api/v1/alarms/statistics", nil))
	if w.Code != 401 {
		t.Fatalf("statistics must require JWT, got %d", w.Code)
	}
}

func NewForStatisticsAuthTest() *gin.Engine {
	r := gin.New()
	s := &Server{}
	r.GET("/api/v1/alarms/statistics", s.authorize, s.alarmStatistics)
	return r
}

func TestStatisticsPostgresBoundariesAndGroups(t *testing.T) {
	dsn := os.Getenv("NATURAL_TEST_POSTGRES_DSN")
	if dsn == "" {
		t.Skip("requires isolated PostgreSQL; set NATURAL_TEST_POSTGRES_DSN")
	}
	db, err := gorm.Open(postgres.Open(dsn), &gorm.Config{})
	if err != nil {
		t.Fatal(err)
	}
	sqlDB, _ := db.DB()
	defer sqlDB.Close()
	tx := db.Begin()
	defer tx.Rollback()
	// Transaction-local shadow tables never alter existing project data.
	for _, sql := range []string{
		"CREATE TEMP TABLE devices(id BIGINT PRIMARY KEY, device_code TEXT, deleted_at TIMESTAMPTZ) ON COMMIT DROP",
		"CREATE TEMP TABLE alarm_records(device_id BIGINT, metric TEXT, level TEXT, status TEXT, triggered_at TIMESTAMPTZ) ON COMMIT DROP",
		"INSERT INTO devices VALUES(1,'INV-1001',NULL),(2,'INV-1002',NULL),(3,'REMOVED',NOW())",
	} {
		if err := tx.Exec(sql).Error; err != nil {
			t.Fatal(err)
		}
	}
	end := time.Date(2026, 10, 9, 12, 0, 0, 0, time.UTC)
	start := end.AddDate(0, 0, -30)
	events := []struct {
		id                    int
		status, metric, level string
		when                  time.Time
	}{
		{1, "recovered", "temperature", "major", start}, {1, "unhandled", "voltage", "minor", end.Add(-time.Second)},
		{2, "acked", "temperature", "major", start.Add(time.Hour)}, {2, "recovered", "temperature", "major", start.Add(time.Second)},
		{1, "unhandled", "temperature", "major", start.Add(-time.Nanosecond)},
		{2, "unhandled", "temperature", "major", end}, {3, "unhandled", "temperature", "major", start.Add(time.Hour)},
	}
	// Postgres timestamp precision is microseconds.
	events[4].when = start.Add(-time.Microsecond)
	for _, event := range events {
		if err := tx.Exec("INSERT INTO alarm_records VALUES(?,?,?,?,?)", event.id, event.metric, event.level, event.status, event.when).Error; err != nil {
			t.Fatal(err)
		}
	}
	q := alarmStatisticsQuery{Group: "device", Start: start, End: end}
	groups, total, err := aggregateAlarms(tx, q)
	if err != nil || total != 4 || len(groups) != 2 || groups[0].Count != 2 || groups[1].Count != 2 {
		t.Fatalf("tie/boundary/recovered: %+v total=%d err=%v", groups, total, err)
	}
	for _, dimension := range []string{"metric", "level", "status"} {
		q.Group = dimension
		groups, total, err = aggregateAlarms(tx, q)
		if err != nil || total != 4 {
			t.Fatalf("%s: total=%d err=%v", dimension, total, err)
		}
	}
	q.Group, q.Status = "device", "recovered"
	groups, total, err = aggregateAlarms(tx, q)
	if err != nil || total != 2 || len(groups) != 2 {
		t.Fatal("recovered events excluded")
	}
	q.DeviceID = 999
	groups, total, err = aggregateAlarms(tx, q)
	if err != nil || total != 0 || len(groups) != 0 {
		t.Fatal("zero case")
	}
	// Exercise response contract, not just aggregate SQL.
	s := &Server{db: tx}
	r := gin.New()
	r.GET("/statistics", s.alarmStatistics)
	w := httptest.NewRecorder()
	r.ServeHTTP(w, httptest.NewRequest("GET", "/statistics?start="+start.Format(time.RFC3339)+"&end="+end.Format(time.RFC3339), nil))
	var body struct {
		Data struct {
			Complete bool                   `json:"complete"`
			Total    int                    `json:"total_events"`
			Groups   []alarmStatisticsGroup `json:"groups"`
		} `json:"data"`
	}
	if json.Unmarshal(w.Body.Bytes(), &body) != nil || w.Code != 200 || !body.Data.Complete || body.Data.Total != 4 || len(body.Data.Groups) != 2 {
		t.Fatalf("response %s", w.Body.String())
	}
}
