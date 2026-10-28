package api

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"github.com/gin-gonic/gin"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	_ "modernc.org/sqlite"

	"powersystem/backend/internal/config"
)

// This opt-in smoke calls real read handlers with isolated SQLite fixture rows.
// It does not validate PostgreSQL dialect compatibility, Redis or authentication.
func TestFleetMinimalSmoke(t *testing.T) {
	manifest := os.Getenv("FLEET_CONFIG_PATH")
	if manifest == "" || os.Getenv("FLEET_SMOKE_OUTPUT") == "" {
		t.Skip("set FLEET_CONFIG_PATH and FLEET_SMOKE_OUTPUT for the isolated smoke")
	}
	manifest, err := filepath.Abs(manifest)
	if err != nil {
		t.Fatal(err)
	}
	conn, err := sql.Open("sqlite", ":memory:")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { conn.Close() })
	conn.SetMaxOpenConns(1)
	for _, statement := range []string{
		`CREATE TABLE devices (id INTEGER PRIMARY KEY, device_code TEXT, name TEXT, dev_type TEXT, vendor TEXT, is_simulated BOOLEAN, reference_vendor TEXT, reference_model TEXT, station_code TEXT, group_name TEXT, deleted_at DATETIME, created_at DATETIME, updated_at DATETIME)`,
		`CREATE TABLE alarm_records (id INTEGER PRIMARY KEY, device_id INTEGER, level TEXT, recovered_at DATETIME, triggered_at DATETIME, archived BOOLEAN)`,
		`CREATE TABLE energy_daily (device_id INTEGER, day TEXT, kwh REAL, segments_count INTEGER, source_quality TEXT DEFAULT 'verified', updated_at DATETIME)`,
	} {
		if _, err := conn.Exec(statement); err != nil {
			t.Fatal(err)
		}
	}
	db, err := gorm.Open(postgres.New(postgres.Config{Conn: conn}), &gorm.Config{})
	if err != nil {
		t.Fatal(err)
	}
	s := New(config.Config{FleetPath: manifest}, db, nil, nil, nil)
	if s.fleetCatalog == nil {
		t.Fatal("explicit fleet path did not load")
	}
	id := 0
	for _, station := range s.fleetCatalog.Stations {
		codes := station.Explicit
		if station.Scope == "business" {
			codes = nil
			for n := 1; n <= station.ExpectedDevices; n++ {
				codes = append(codes, fmt.Sprintf("%s-%s-%02d", station.Code, station.DevicePrefix, n))
			}
		}
		for _, code := range codes {
			id++
			_, err := conn.Exec(`INSERT INTO devices (id,device_code,name,dev_type,vendor,is_simulated,reference_vendor,reference_model,station_code,group_name) VALUES (?,?,?,'inverter','smoke',TRUE,'','',?,'smoke')`, id, code, code, station.Code)
			if err != nil {
				t.Fatal(err)
			}
		}
	}
	if id != 7 {
		t.Fatalf("smoke requires seven devices, got %d", id)
	}
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.GET("/api/v1/stations", s.listStations)
	r.GET("/api/v1/devices", s.listDevices)
	r.GET("/api/v1/alarms", s.listAlarms)
	r.GET("/api/v1/dashboard/overview", s.dashboardOverview)
	evidence := map[string]json.RawMessage{}
	for _, tc := range []struct {
		scope string
		count int
	}{{"all", 7}, {"business", 4}, {"baseline", 3}} {
		for _, endpoint := range []string{"stations", "devices", "alarms", "dashboard/overview"} {
			url := "/api/v1/" + endpoint + "?scope=" + tc.scope
			w := httptest.NewRecorder()
			r.ServeHTTP(w, httptest.NewRequest("GET", url, nil))
			if w.Code != 200 {
				t.Fatalf("%s: %d %s", url, w.Code, w.Body.String())
			}
			var response struct {
				Code int                        `json:"code"`
				Data map[string]json.RawMessage `json:"data"`
			}
			if err := json.Unmarshal(w.Body.Bytes(), &response); err != nil {
				t.Fatal(err)
			}
			if response.Code != 0 {
				t.Fatalf("%s: %s", url, w.Body.String())
			}
			if endpoint == "devices" {
				var total int
				json.Unmarshal(response.Data["total"], &total)
				if total != tc.count {
					t.Fatalf("%s total=%d", url, total)
				}
			}
			if endpoint == "dashboard/overview" {
				for _, field := range []string{"expected_device_count", "registered_device_count"} {
					var count int
					json.Unmarshal(response.Data[field], &count)
					if count != tc.count {
						t.Fatalf("%s %s=%d", url, field, count)
					}
				}
			}
			evidence[endpoint+"?scope="+tc.scope] = append([]byte(nil), w.Body.Bytes()...)
		}
	}
	w := httptest.NewRecorder()
	r.ServeHTTP(w, httptest.NewRequest("GET", "/api/v1/devices?scope=business&station_code=PV-04", nil))
	var filtered struct {
		Data struct {
			Total int `json:"total"`
		} `json:"data"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &filtered); err != nil {
		t.Fatal(err)
	}
	if w.Code != 200 || filtered.Data.Total != 2 {
		t.Fatal("station filter did not return two devices")
	}
	encoded, err := json.MarshalIndent(evidence, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(os.Getenv("FLEET_SMOKE_OUTPUT"), encoded, 0600); err != nil {
		t.Fatal(err)
	}
	t.Log("PASS: explicit manifest, seven fixture rows, 13 real handler requests; Redis absent => partial/unknown observations")
}
