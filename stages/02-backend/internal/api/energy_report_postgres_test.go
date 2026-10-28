package api

import (
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"math"
	"net/url"
	"os"
	"strings"
	"testing"
	"time"
)

func TestEnergyReportRealPostgres(t *testing.T) {
	dsn := os.Getenv("S02_ENERGY_TEST_DATABASE_URL")
	if s03 := os.Getenv("S03_ENERGY_TEST_DATABASE_URL"); s03 != "" {
		dsn = s03
	}
	if dsn == "" {
		dsn = os.Getenv("TR_S02_TEST_DSN")
	}
	if dsn == "" {
		t.Skip("dedicated S02 PostgreSQL not configured")
	}
	u, err := url.Parse(dsn)
	isolated := err == nil && u.Hostname() == "127.0.0.1" && strings.HasSuffix(u.Path, "_test") &&
		((u.Port() == "25432" && strings.HasPrefix(u.Path, "/v4_s02_")) || (u.Port() == "25433" && strings.HasPrefix(u.Path, "/v4_s03_")))
	isolated = isolated || dsn == "postgres://tr_s02@127.0.0.1:15440/tr_s02_test?sslmode=disable"
	if !isolated {
		t.Fatal("dedicated S02/S03 loopback test database required")
	}
	db, err := gorm.Open(postgres.Open(dsn), &gorm.Config{})
	if err != nil {
		t.Fatal(err)
	}
	pool, _ := db.DB()
	defer pool.Close()
	tx := db.Begin()
	if tx.Error != nil {
		t.Fatal(tx.Error)
	}
	defer tx.Rollback()
	for _, q := range []string{
		`CREATE TEMP TABLE devices (id BIGINT PRIMARY KEY,device_code TEXT,name TEXT,dev_type TEXT,vendor TEXT,is_simulated BOOLEAN,reference_vendor TEXT,reference_model TEXT,station_code TEXT,group_name TEXT,deleted_at TIMESTAMPTZ,created_at TIMESTAMPTZ,updated_at TIMESTAMPTZ) ON COMMIT DROP`,
		`CREATE TEMP TABLE energy_daily(device_id BIGINT,day DATE,kwh DOUBLE PRECISION,segments_count INTEGER,source_quality TEXT DEFAULT 'verified',updated_at TIMESTAMPTZ,PRIMARY KEY(device_id,day)) ON COMMIT DROP`,
	} {
		if err = tx.Exec(q).Error; err != nil {
			t.Fatal(err)
		}
	}
	seedEnergyDevice(t, tx, 11, "SYN-INV-01", "PV-01")
	seedEnergyDevice(t, tx, 28, "SYN-INV-02", "PV-01")
	updated := time.Date(2026, 10, 8, 1, 0, 0, 0, time.UTC)
	for d := 1; d <= 7; d++ {
		seedEnergyDaily(t, tx, 28, time.Date(2026, 10, d, 0, 0, 0, 0, time.UTC).Format("2006-01-02"), 0.014, 1, updated)
	}
	seedEnergyDaily(t, tx, 28, "2026-10-08", 9999, 1, updated)
	r := energyReportRouter(&Server{db: tx, fleetCatalog: testFleetCatalog(t)})
	_, report := getEnergyReport(t, r, "?start_date=2026-10-01&end_date=2026-10-08&device_id=28")
	if !report.Complete || report.TotalKWh == nil || math.Abs(*report.TotalKWh-0.098) > 1e-10 || len(report.Daily) != 7 {
		t.Fatalf("DATE/end-exclusive/precision mismatch: %+v", report)
	}
	_, empty := getEnergyReport(t, r, "?start_date=2026-10-01&end_date=2026-10-08&scope=baseline&station_code=PV-01&device_id=28")
	if empty.Complete || len(empty.Devices) != 0 || empty.TotalKWh != nil {
		t.Fatalf("disjoint filter escaped: %+v", empty)
	}
	if err = tx.Exec(`UPDATE energy_daily SET kwh='NaN' WHERE device_id=28 AND day='2026-10-02'`).Error; err != nil {
		t.Fatal(err)
	}
	_, partial := getEnergyReport(t, r, "?start_date=2026-10-01&end_date=2026-10-08&device_id=28")
	if partial.Complete || len(partial.Missing) != 1 || partial.Missing[0].Reason != "invalid_value" {
		t.Fatalf("nonfinite not rejected: %+v", partial)
	}
}
