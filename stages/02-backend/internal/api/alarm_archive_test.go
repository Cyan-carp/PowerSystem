package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
)

func TestAlarmArchiveHidesRecordsWithoutDeletingThem(t *testing.T) {
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
	if err := tx.Exec(`CREATE TEMP TABLE alarm_records(
id BIGINT PRIMARY KEY,device_id BIGINT NOT NULL,rule_id BIGINT,metric TEXT NOT NULL,level TEXT NOT NULL,
value DOUBLE PRECISION NOT NULL,threshold DOUBLE PRECISION NOT NULL,status TEXT NOT NULL,archived BOOLEAN NOT NULL DEFAULT FALSE,
close_reason TEXT,rule_operator TEXT,rule_version TEXT,acked_by BIGINT,acked_at TIMESTAMPTZ,recovered_at TIMESTAMPTZ,
triggered_at TIMESTAMPTZ NOT NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
) ON COMMIT DROP`).Error; err != nil {
		t.Fatal(err)
	}
	now := time.Now().UTC()
	if err := tx.Exec(`INSERT INTO alarm_records(id,device_id,metric,level,value,threshold,status,archived,triggered_at)
VALUES (1,10,'temperature','major',72,65,'recovered',FALSE,?),(2,11,'voltage','minor',450,440,'recovered',TRUE,?)`, now, now.Add(-time.Minute)).Error; err != nil {
		t.Fatal(err)
	}

	s := &Server{db: tx}
	r := gin.New()
	r.GET("/alarms", s.listAlarms)
	r.GET("/alarms/:id", s.getAlarm)
	r.POST("/alarms/:id/archive", s.archiveAlarm)

	assertVisibleAlarmTotal(t, r, 1)
	w := httptest.NewRecorder()
	r.ServeHTTP(w, httptest.NewRequest(http.MethodPost, "/alarms/1/archive", nil))
	if w.Code != http.StatusOK {
		t.Fatalf("archive returned %d: %s", w.Code, w.Body.String())
	}
	assertVisibleAlarmTotal(t, r, 0)

	w = httptest.NewRecorder()
	r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/alarms/1", nil))
	var detail struct {
		Data struct {
			ID       int64 `json:"id"`
			Archived bool  `json:"archived"`
		} `json:"data"`
	}
	if json.Unmarshal(w.Body.Bytes(), &detail) != nil || w.Code != http.StatusOK || detail.Data.ID != 1 || !detail.Data.Archived {
		t.Fatalf("archived record was not retained: %s", w.Body.String())
	}
}

func assertVisibleAlarmTotal(t *testing.T, handler http.Handler, expected int64) {
	t.Helper()
	w := httptest.NewRecorder()
	handler.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/alarms?page=1&page_size=20", nil))
	var body struct {
		Data struct {
			Total int64 `json:"total"`
		} `json:"data"`
	}
	if json.Unmarshal(w.Body.Bytes(), &body) != nil || w.Code != http.StatusOK || body.Data.Total != expected {
		t.Fatalf("visible alarm total=%d, want %d: %s", body.Data.Total, expected, w.Body.String())
	}
}
