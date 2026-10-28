package api

import (
	"database/sql"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	_ "modernc.org/sqlite"

	"powersystem/backend/internal/config"
)

// energyReportDB 在内存 SQLite 上复现 devices 与 energy_daily 两张只读表。
// 只验证 Go 侧取数与聚合口径，不代替真实 PostgreSQL 复核。
func energyReportDB(t *testing.T) *gorm.DB {
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
		`CREATE TABLE devices (id INTEGER PRIMARY KEY, device_code TEXT, name TEXT, dev_type TEXT, vendor TEXT, is_simulated BOOLEAN, reference_vendor TEXT, reference_model TEXT, station_code TEXT, group_name TEXT, deleted_at DATETIME, created_at DATETIME, updated_at DATETIME)`,
		`CREATE TABLE energy_dirty_days(device_id INTEGER,day TEXT,changed_at DATETIME)`,
		`CREATE TABLE energy_daily (device_id INTEGER, day TEXT, kwh REAL, segments_count INTEGER, source_quality TEXT DEFAULT 'verified', covered_ms INTEGER, gap_count INTEGER, max_gap_ms INTEGER, updated_at DATETIME)`,
	} {
		if err := db.Exec(query).Error; err != nil {
			t.Fatal(err)
		}
	}
	return db
}

func seedEnergyDevice(t *testing.T, db *gorm.DB, id int64, code, station string) {
	t.Helper()
	if err := db.Exec(`INSERT INTO devices (id,device_code,name,dev_type,vendor,is_simulated,station_code,group_name) VALUES (?,?,?,'inverter','fixture',TRUE,?,'test')`, id, code, code, station).Error; err != nil {
		t.Fatal(err)
	}
}

func seedEnergyDaily(t *testing.T, db *gorm.DB, deviceID int64, day string, kwh any, segments int64, updated time.Time) {
	t.Helper()
	if err := db.Exec(`INSERT INTO energy_daily (device_id,day,kwh,segments_count,updated_at) VALUES (?,?,?,?,?)`, deviceID, day, kwh, segments, updated).Error; err != nil {
		t.Fatal(err)
	}
}

func energyReportRouter(s *Server) *gin.Engine {
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.GET("/reports/energy", s.energyReport)
	return r
}

func getEnergyReport(t *testing.T, r *gin.Engine, query string) (int, energyReport) {
	t.Helper()
	w := httptest.NewRecorder()
	r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/reports/energy"+query, nil))
	var envelope struct {
		Code int          `json:"code"`
		Data energyReport `json:"data"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &envelope); err != nil {
		t.Fatalf("decode %s: %v", w.Body.String(), err)
	}
	return w.Code, envelope.Data
}

func energyReportDeviceIDs(report energyReport) []int64 {
	ids := make([]int64, len(report.Devices))
	for i, device := range report.Devices {
		ids[i] = device.DeviceID
	}
	return ids
}

func TestEnergyReportRouteRequiresAuth(t *testing.T) {
	s := New(config.Config{}, nil, nil, nil, nil)
	w := httptest.NewRecorder()
	s.Handler().ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/api/v1/reports/energy?start_date=2026-10-01&end_date=2026-10-02", nil))
	if w.Code != http.StatusUnauthorized {
		t.Fatalf("energy report must stay behind JWT auth, got %d: %s", w.Code, w.Body.String())
	}
}

func TestEnergyReportWindowValidation(t *testing.T) {
	db := energyReportDB(t)
	s := &Server{db: db, fleetCatalog: testFleetCatalog(t)}
	r := energyReportRouter(s)
	today := time.Now().In(energyReportZone)
	todayText := today.Format(energyReportDayLayout)
	tomorrow := today.AddDate(0, 0, 1).Format(energyReportDayLayout)
	afterTomorrow := today.AddDate(0, 0, 2).Format(energyReportDayLayout)

	for _, tc := range []struct {
		name   string
		query  string
		status int
	}{
		{"missing window", "", http.StatusBadRequest},
		{"missing end", "?start_date=2026-10-01", http.StatusBadRequest},
		{"missing start", "?end_date=2026-10-02", http.StatusBadRequest},
		{"bad format", "?start_date=2026/10/01&end_date=2026-10-02", http.StatusBadRequest},
		{"timestamp not a day", "?start_date=2026-10-01T00:00:00Z&end_date=2026-10-02", http.StatusBadRequest},
		{"normalized calendar date", "?start_date=2026-02-31&end_date=2026-03-05", http.StatusBadRequest},
		{"empty window", "?start_date=2026-10-01&end_date=2026-10-01", http.StatusBadRequest},
		{"reversed window", "?start_date=2026-10-08&end_date=2026-10-01", http.StatusBadRequest},
		{"32 day window", "?start_date=2026-08-31&end_date=2026-10-02", http.StatusBadRequest},
		{"31 day window", "?start_date=2026-09-01&end_date=2026-10-02", http.StatusOK},
		{"today allowed", "?start_date=" + todayText + "&end_date=" + tomorrow, http.StatusOK},
		{"future end rejected", "?start_date=" + todayText + "&end_date=" + afterTomorrow, http.StatusBadRequest},
		{"invalid scope", "?start_date=2026-10-01&end_date=2026-10-02&scope=ops", http.StatusBadRequest},
		{"invalid station code", "?start_date=2026-10-01&end_date=2026-10-02&station_code=bad+code", http.StatusBadRequest},
		{"station scope disjoint intersection", "?start_date=2026-10-01&end_date=2026-10-02&scope=business&station_code=ST-01", http.StatusOK},
		{"invalid device id", "?start_date=2026-10-01&end_date=2026-10-02&device_id=abc", http.StatusBadRequest},
		{"zero device id", "?start_date=2026-10-01&end_date=2026-10-02&device_id=0", http.StatusBadRequest},
	} {
		t.Run(tc.name, func(t *testing.T) {
			status, _ := getEnergyReport(t, r, tc.query)
			if status != tc.status {
				t.Fatalf("status=%d want %d", status, tc.status)
			}
		})
	}

	// business/baseline 需要清单；清单不可用是 503，不是空结果。
	noCatalog := energyReportRouter(&Server{db: db})
	if status, _ := getEnergyReport(t, noCatalog, "?start_date=2026-10-01&end_date=2026-10-02&scope=business"); status != http.StatusServiceUnavailable {
		t.Fatalf("business scope without fleet catalog: %d", status)
	}
	if status, _ := getEnergyReport(t, noCatalog, "?start_date=2026-10-01&end_date=2026-10-02"); status != http.StatusOK {
		t.Fatalf("all scope must work without fleet catalog: %d", status)
	}
}

func TestEnergyReportScopeAndDeviceIntersection(t *testing.T) {
	db := energyReportDB(t)
	seedEnergyDevice(t, db, 1, "PV-01-INV-01", "PV-01")
	seedEnergyDevice(t, db, 2, "PV-01-INV-02", "PV-01")
	seedEnergyDevice(t, db, 3, "PV-02-INV-01", "PV-02")
	seedEnergyDevice(t, db, 4, "ST-01-INV-01", "ST-01")
	seedEnergyDevice(t, db, 5, "PV-04-INV-01", "PV-04")
	if err := db.Exec(`UPDATE devices SET deleted_at='2026-10-01 00:00:00' WHERE id=5`).Error; err != nil {
		t.Fatal(err)
	}
	r := energyReportRouter(&Server{db: db, fleetCatalog: testFleetCatalog(t)})
	window := "?start_date=2026-10-01&end_date=2026-10-02"

	for _, tc := range []struct {
		name    string
		query   string
		wantIDs []int64
	}{
		{"default all excludes deleted", window, []int64{1, 2, 3, 4}},
		{"business", window + "&scope=business", []int64{1, 2, 3}},
		{"baseline", window + "&scope=baseline", []int64{4}},
		{"station filter", window + "&station_code=PV-01", []int64{1, 2}},
		{"business station", window + "&scope=business&station_code=PV-02", []int64{3}},
		{"device inside scope", window + "&scope=business&device_id=3", []int64{3}},
		{"device outside scope", window + "&scope=business&device_id=4", nil},
		{"device outside station", window + "&station_code=PV-01&device_id=3", nil},
		{"baseline device", window + "&scope=baseline&device_id=4", []int64{4}},
		{"deleted device", window + "&device_id=5", nil},
		{"unknown device", window + "&device_id=99", nil},
	} {
		t.Run(tc.name, func(t *testing.T) {
			status, report := getEnergyReport(t, r, tc.query)
			if status != http.StatusOK {
				t.Fatalf("status=%d", status)
			}
			got := energyReportDeviceIDs(report)
			if len(got) != len(tc.wantIDs) {
				t.Fatalf("devices=%v want %v", got, tc.wantIDs)
			}
			for i := range got {
				if got[i] != tc.wantIDs[i] {
					t.Fatalf("devices=%v want %v", got, tc.wantIDs)
				}
			}
			if len(tc.wantIDs) == 0 && (report.Complete || report.TotalKWh != nil) {
				t.Fatalf("empty intersection must not claim completeness or zero: %+v", report)
			}
		})
	}
}

func TestEnergyReportSevenDayAggregationAndExclusiveEnd(t *testing.T) {
	db := energyReportDB(t)
	seedEnergyDevice(t, db, 1, "PV-01-INV-01", "PV-01")
	seedEnergyDevice(t, db, 2, "PV-01-INV-02", "PV-01")
	updated := time.Date(2026, 10, 8, 0, 30, 0, 0, time.UTC)
	for day := 1; day <= 7; day++ {
		stamp := time.Date(2026, 10, day, 0, 0, 0, 0, energyReportZone).Format(energyReportDayLayout)
		seedEnergyDaily(t, db, 1, stamp, 10.0, 40, updated)
		seedEnergyDaily(t, db, 2, stamp, 2.5, 40, updated)
	}
	// 窗口外：前一日与不含的结束日都不得计入。
	seedEnergyDaily(t, db, 1, "2026-09-30", 500.0, 40, updated)
	seedEnergyDaily(t, db, 1, "2026-10-08", 999.0, 40, updated)
	seedEnergyDaily(t, db, 1, "2026-10-09", 999.0, 40, updated)

	r := energyReportRouter(&Server{db: db, fleetCatalog: testFleetCatalog(t)})
	status, report := getEnergyReport(t, r, "?start_date=2026-10-01&end_date=2026-10-08&scope=business")
	if status != http.StatusOK {
		t.Fatalf("status=%d", status)
	}
	if report.Days != 7 || len(report.Daily) != 7 {
		t.Fatalf("window=%d daily=%d", report.Days, len(report.Daily))
	}
	if report.StartDate != "2026-10-01" || report.EndDate != "2026-10-08" {
		t.Fatalf("window echo start=%s end=%s", report.StartDate, report.EndDate)
	}
	if report.Daily[0].Date != "2026-10-01" || report.Daily[6].Date != "2026-10-07" {
		t.Fatalf("daily boundaries %s..%s", report.Daily[0].Date, report.Daily[6].Date)
	}
	for _, day := range report.Daily {
		if day.KWh == nil || *day.KWh != 12.5 || day.ValidDeviceCount != 2 || !day.Complete {
			t.Fatalf("unexpected day aggregate %+v", day)
		}
	}
	if report.TotalKWh == nil || *report.TotalKWh != 87.5 {
		t.Fatalf("total=%v", report.TotalKWh)
	}
	if !report.Complete || len(report.Missing) != 0 {
		t.Fatalf("complete=%v missing=%+v", report.Complete, report.Missing)
	}
	if report.AggregatedAt == nil || *report.AggregatedAt != updated.Format(time.RFC3339Nano) {
		t.Fatalf("aggregated_at=%v", report.AggregatedAt)
	}
	if report.RuleVersion != energyReportRuleVersion || report.ComputedAt == "" {
		t.Fatalf("rule=%s computed_at=%q", report.RuleVersion, report.ComputedAt)
	}
}

func TestEnergyReportDistinguishesZeroFromMissing(t *testing.T) {
	db := energyReportDB(t)
	seedEnergyDevice(t, db, 1, "PV-01-INV-01", "PV-01")
	updated := time.Date(2026, 10, 8, 1, 0, 0, 0, time.UTC)
	seedEnergyDaily(t, db, 1, "2026-10-01", 0.0, 12, updated)
	seedEnergyDaily(t, db, 1, "2026-10-02", 0.0, 0, updated)
	seedEnergyDaily(t, db, 1, "2026-10-04", nil, 7, updated)

	r := energyReportRouter(&Server{db: db, fleetCatalog: testFleetCatalog(t)})
	status, report := getEnergyReport(t, r, "?start_date=2026-10-01&end_date=2026-10-05")
	if status != http.StatusOK {
		t.Fatalf("status=%d", status)
	}
	if report.Daily[0].KWh == nil || *report.Daily[0].KWh != 0 || !report.Daily[0].Complete {
		t.Fatalf("valid zero segment must stay zero and complete: %+v", report.Daily[0])
	}
	if report.Daily[1].KWh != nil || report.Daily[1].Complete {
		t.Fatalf("zero segments must not be zero energy: %+v", report.Daily[1])
	}
	if report.Daily[2].KWh != nil || report.Daily[2].Complete {
		t.Fatalf("absent row must not be zero energy: %+v", report.Daily[2])
	}
	if report.Daily[3].KWh != nil {
		t.Fatalf("null total must not be zero energy: %+v", report.Daily[3])
	}
	if report.Complete {
		t.Fatal("partial window must not be complete")
	}
	if report.TotalKWh == nil || *report.TotalKWh != 0 {
		t.Fatalf("total should keep the valid zero and flag incompleteness: %v", report.TotalKWh)
	}
	want := []energyReportMissing{
		{DeviceID: 1, DeviceCode: "PV-01-INV-01", Date: "2026-10-02", Reason: "no_segments"},
		{DeviceID: 1, DeviceCode: "PV-01-INV-01", Date: "2026-10-03", Reason: "no_record"},
		{DeviceID: 1, DeviceCode: "PV-01-INV-01", Date: "2026-10-04", Reason: "no_value"},
	}
	if len(report.Missing) != len(want) {
		t.Fatalf("missing=%+v", report.Missing)
	}
	for i := range want {
		if report.Missing[i] != want[i] {
			t.Fatalf("missing[%d]=%+v want %+v", i, report.Missing[i], want[i])
		}
	}
}

func TestEnergyReportNoDevicesInScope(t *testing.T) {
	db := energyReportDB(t)
	seedEnergyDevice(t, db, 1, "PV-01-INV-01", "PV-01")
	r := energyReportRouter(&Server{db: db, fleetCatalog: testFleetCatalog(t)})
	status, report := getEnergyReport(t, r, "?start_date=2026-10-01&end_date=2026-10-04&scope=baseline")
	if status != http.StatusOK {
		t.Fatalf("status=%d", status)
	}
	if len(report.Devices) != 0 || len(report.Missing) != 0 {
		t.Fatalf("devices=%v missing=%+v", report.Devices, report.Missing)
	}
	if report.Complete || report.TotalKWh != nil || report.AggregatedAt != nil {
		t.Fatalf("empty scope must not claim a result: %+v", report)
	}
	if len(report.Daily) != 3 {
		t.Fatalf("daily=%d", len(report.Daily))
	}
	for _, day := range report.Daily {
		if day.KWh != nil || day.Complete || day.DeviceCount != 0 {
			t.Fatalf("empty scope day=%+v", day)
		}
	}
}

func TestEnergyReportNoAggregatedRecords(t *testing.T) {
	db := energyReportDB(t)
	seedEnergyDevice(t, db, 1, "PV-01-INV-01", "PV-01")
	r := energyReportRouter(&Server{db: db, fleetCatalog: testFleetCatalog(t)})
	status, report := getEnergyReport(t, r, "?start_date=2026-10-01&end_date=2026-10-03&device_id=1")
	if status != http.StatusOK {
		t.Fatalf("status=%d", status)
	}
	if report.DeviceID == nil || *report.DeviceID != 1 {
		t.Fatalf("device echo=%v", report.DeviceID)
	}
	if report.Complete || report.TotalKWh != nil || report.AggregatedAt != nil {
		t.Fatalf("no records must not be reported as zero: %+v", report)
	}
	if len(report.Missing) != 2 {
		t.Fatalf("missing=%+v", report.Missing)
	}
	for _, item := range report.Missing {
		if item.Reason != "no_record" || item.DeviceID != 1 {
			t.Fatalf("missing=%+v", item)
		}
	}
	for _, day := range report.Daily {
		if day.KWh != nil || day.ValidDeviceCount != 0 || day.Complete {
			t.Fatalf("day=%+v", day)
		}
	}
}

func TestEnergyReportDirtyNeverComplete(t *testing.T) {
	db := energyReportDB(t)
	seedEnergyDevice(t, db, 1, "INV-1001", "ST-01")
	seedEnergyDaily(t, db, 1, "2026-10-01", 1.0, 1, time.Now())
	if e := db.Exec("UPDATE energy_daily SET covered_ms=86400000,gap_count=0,max_gap_ms=5000").Error; e != nil {
		t.Fatal(e)
	}
	if e := db.Exec("INSERT INTO energy_dirty_days(device_id,day) VALUES(1,'2026-10-01')").Error; e != nil {
		t.Fatal(e)
	}
	server := &Server{db: db, fleetCatalog: testFleetCatalog(t)}
	status, report := getEnergyReport(t, energyReportRouter(server), "?start_date=2026-10-01&end_date=2026-10-02")
	if status != 200 || report.Complete || report.Daily[0].CoverageComplete {
		t.Fatal("dirty presented as complete", status, report)
	}
	if len(report.Missing) != 1 || report.Missing[0].Reason != "dirty_pending" {
		t.Fatal("dirty provenance hidden", report.Missing)
	}
}
