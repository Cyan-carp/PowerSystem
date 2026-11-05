package service

import (
	"context"
	"database/sql"
	"testing"
	"time"

	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	_ "modernc.org/sqlite"
)

func truthTestDB(t *testing.T) *gorm.DB {
	t.Helper()
	conn, err := sql.Open("sqlite", ":memory:")
	if err != nil {
		t.Fatal(err)
	}
	db, err := gorm.Open(postgres.New(postgres.Config{Conn: conn}), &gorm.Config{
		Logger: logger.Default.LogMode(logger.Silent),
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := db.Exec(`CREATE TABLE simulation_alarm_policy_devices (
		station_code TEXT NOT NULL,
		device_code TEXT NOT NULL,
		active BOOLEAN NOT NULL
	)`).Error; err != nil {
		t.Fatal(err)
	}
	return db
}

func TestWindowFaultUsesStrictOneHourHorizon(t *testing.T) {
	db := truthTestDB(t)
	codes := []string{"PV-02-INV-01", "PV-02-INV-02"}
	for _, code := range codes {
		if err := db.Exec(`INSERT INTO simulation_alarm_policy_devices(station_code, device_code, active)
			VALUES (?, ?, ?)`, "PV-02", code, true).Error; err != nil {
			t.Fatal(err)
		}
	}
	day := time.Date(2026, 11, 4, 0, 0, 0, 0, SimulationLocation())
	onset := SimulationScheduledAt(day, 11).UnixMilli()
	selected := codes[SimulationDeviceIndex(day, len(codes))]
	other := codes[(SimulationDeviceIndex(day, len(codes))+1)%len(codes)]
	cache := NewStationDeviceCache(db, context.Background())

	cases := []struct {
		name       string
		device     string
		windowEnd  int64
		wantFault  bool
		wantOffset int64
	}{
		{"exactly one hour before onset is inside", selected, onset - PredictionHorizonMS, true, onset},
		{"one millisecond before the hour is outside", selected, onset - PredictionHorizonMS - 1, false, 0},
		{"ninety minutes before onset is outside", selected, onset - 90*60_000, false, 0},
		{"window ending at onset is left-open", selected, onset, false, 0},
		{"thirty minutes before onset is inside", selected, onset - 30*60_000, true, onset},
		{"same station but other device is not a hit", other, onset - 30*60_000, false, 0},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			fault, gotOnset, err := cache.WindowFault(tc.device, "PV-02", tc.windowEnd)
			if err != nil {
				t.Fatal(err)
			}
			if fault != tc.wantFault {
				t.Fatalf("fault = %v, want %v", fault, tc.wantFault)
			}
			if !tc.wantFault {
				if gotOnset != nil {
					t.Fatalf("onset = %d, want nil", *gotOnset)
				}
				return
			}
			if gotOnset == nil || *gotOnset != tc.wantOffset {
				t.Fatalf("onset = %v, want %d", gotOnset, tc.wantOffset)
			}
		})
	}
}
