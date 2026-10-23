package service

import (
	"database/sql"
	"encoding/json"
	"testing"
	"time"

	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	_ "modernc.org/sqlite"

	"powersystem/backend/internal/fleet"
)

func TestSimulationScheduleFiveSlots(t *testing.T) {
	day := time.Date(2026, 10, 21, 8, 30, 0, 0, SimulationLocation())
	plans := SimulationSchedule(day)
	if len(plans) != 5 {
		t.Fatalf("got %d slots, want 5", len(plans))
	}
	wantHours := []int{9, 11, 13, 15, 17}
	wantStations := []string{"PV-01", "PV-02", "PV-03", "PV-04", "ST-01"}
	for index, plan := range plans {
		if plan.Hour != wantHours[index] || plan.StationCode != wantStations[index] {
			t.Fatalf("slot %d = %d/%s, want %d/%s", index, plan.Hour, plan.StationCode, wantHours[index], wantStations[index])
		}
		scheduled := SimulationScheduledAt(plan.Day, plan.Hour)
		if scheduled.Hour() != plan.Hour || scheduled.Minute() != 0 || scheduled.Second() != 0 {
			t.Fatalf("scheduled %s does not match hour %d", scheduled, plan.Hour)
		}
		if _, offset := scheduled.Zone(); offset != 8*60*60 {
			t.Fatalf("scheduled offset = %d, want UTC+8", offset)
		}
	}
}

func TestSimulationScheduleUsesShanghaiDay(t *testing.T) {
	utcMorning := time.Date(2026, 10, 21, 1, 0, 0, 0, time.UTC) // 09:00 Shanghai
	localDay := time.Date(2026, 10, 21, 9, 0, 0, 0, SimulationLocation())
	if !SimulationDay(utcMorning).Equal(SimulationDay(localDay)) {
		t.Fatalf("same Shanghai day resolved differently: %s vs %s", SimulationDay(utcMorning), SimulationDay(localDay))
	}
	if got := SimulationScheduledAt(utcMorning, 9); got.Hour() != 9 {
		t.Fatalf("hour mismatch: %s", got)
	}
}

func TestSimulationDeviceIndexIsStableAndRotates(t *testing.T) {
	day := time.Date(2026, 10, 21, 0, 0, 0, 0, SimulationLocation())
	first := SimulationDeviceIndex(day, 48)
	if again := SimulationDeviceIndex(day.Add(6*time.Hour), 48); again != first {
		t.Fatalf("same day index changed: %d -> %d", first, again)
	}
	if first < 0 || first >= 48 {
		t.Fatalf("index out of range: %d", first)
	}
	next := SimulationDeviceIndex(day.AddDate(0, 0, 1), 48)
	if next != (first+1)%48 {
		t.Fatalf("next day index %d, want %d", next, (first+1)%48)
	}
	if got := SimulationDeviceIndex(day, 0); got != 0 {
		t.Fatalf("zero devices index = %d", got)
	}
}

func TestSimulationCatalogHas143Devices(t *testing.T) {
	catalog, err := fleet.Load("../../../01-data-chain/simulator/fleet.json")
	if err != nil {
		t.Fatal(err)
	}
	identities, err := SimulationCatalogIdentities(catalog)
	if err != nil {
		t.Fatal(err)
	}
	if len(identities) != SimulationExpectedDevices {
		t.Fatalf("identities = %d, want %d", len(identities), SimulationExpectedDevices)
	}
	if identities[0].DeviceCode != "PV-01-INV-01" || identities[0].StationCode != "PV-01" {
		t.Fatalf("first identity = %+v", identities[0])
	}
	last := identities[len(identities)-1]
	if last.DeviceCode != "INV-1003" || last.StationCode != "ST-01" {
		t.Fatalf("last identity = %+v", last)
	}
	baseline := 0
	for _, identity := range identities {
		if identity.StationCode == "ST-01" {
			baseline++
		}
	}
	if baseline != 3 {
		t.Fatalf("ST-01 identities = %d, want 3", baseline)
	}
	if _, err := SimulationCatalogIdentities(nil); err == nil {
		t.Fatal("nil catalog must fail")
	}
}

func TestSimulationLatestUsable(t *testing.T) {
	now := time.Date(2026, 10, 21, 9, 0, 0, 0, time.UTC)
	payload := func(received time.Time, ts int64, status, fault int) []byte {
		raw, err := json.Marshal(map[string]any{
			"ts_ms": ts, "received_at": received, "status": status, "fault_code": fault,
		})
		if err != nil {
			t.Fatal(err)
		}
		return raw
	}
	if ok, reason := SimulationLatestUsable(payload(now.Add(-time.Minute), now.Add(-time.Minute).UnixMilli(), 1, 0), now); !ok {
		t.Fatalf("fresh telemetry rejected: %s", reason)
	}
	if ok, reason := SimulationLatestUsable(payload(now.Add(-3*time.Minute), now.Add(-time.Minute).UnixMilli(), 1, 0), now); ok || reason != "stale_telemetry" {
		t.Fatalf("stale receive = %v/%q", ok, reason)
	}
	if ok, reason := SimulationLatestUsable(payload(now, now.Add(-3*time.Minute).UnixMilli(), 1, 0), now); ok || reason != "stale_telemetry" {
		t.Fatalf("stale sample = %v/%q", ok, reason)
	}
	if ok, reason := SimulationLatestUsable(payload(now, now.UnixMilli(), 2, 0), now); ok || reason != "device_offline" {
		t.Fatalf("offline = %v/%q", ok, reason)
	}
	if ok, reason := SimulationLatestUsable(payload(now, now.UnixMilli(), 1, 7), now); ok || reason != "device_fault" {
		t.Fatalf("fault = %v/%q", ok, reason)
	}
	if ok, reason := SimulationLatestUsable([]byte("{"), now); ok || reason != "invalid_latest_telemetry" {
		t.Fatalf("invalid = %v/%q", ok, reason)
	}
}

func openSimulationTestDB(t *testing.T) *gorm.DB {
	t.Helper()
	conn, err := sql.Open("sqlite", ":memory:")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { conn.Close() })
	conn.SetMaxOpenConns(1)
	db, err := gorm.Open(postgres.New(postgres.Config{Conn: conn}), &gorm.Config{})
	if err != nil {
		t.Fatal(err)
	}
	for _, statement := range []string{
		`CREATE TABLE devices (id INTEGER PRIMARY KEY, device_code TEXT, name TEXT, dev_type TEXT, station_code TEXT, deleted_at DATETIME)`,
		`CREATE TABLE simulation_alarm_policy_devices (
			id INTEGER PRIMARY KEY AUTOINCREMENT, station_code TEXT NOT NULL, device_code TEXT NOT NULL,
			device_id INTEGER NOT NULL, source TEXT NOT NULL, active BOOLEAN NOT NULL,
			verified_at DATETIME, created_at DATETIME, updated_at DATETIME,
			UNIQUE(station_code, device_code))`,
		`CREATE TABLE simulation_alarm_state (id INTEGER PRIMARY KEY, first_enabled_at DATETIME, updated_at DATETIME)`,
		`CREATE TABLE simulation_alarm_slots (
			id INTEGER PRIMARY KEY AUTOINCREMENT, slot_date TEXT NOT NULL, slot_hour INTEGER NOT NULL,
			station_code TEXT NOT NULL, device_id INTEGER NOT NULL, device_code TEXT NOT NULL,
			status TEXT NOT NULL, reason TEXT, alarm_id INTEGER, scheduled_at DATETIME,
			started_at DATETIME, ended_at DATETIME, created_at DATETIME, updated_at DATETIME,
			UNIQUE(slot_date, slot_hour))`,
	} {
		if err := db.Exec(statement).Error; err != nil {
			t.Fatal(err)
		}
	}
	return db
}

func TestSimulationPolicyPersistsOnlyVerifiedDevices(t *testing.T) {
	db := openSimulationTestDB(t)
	for id, code := range []string{"INV-1001", "INV-1002", "INV-1003", "INV-1004"} {
		if err := db.Exec(`INSERT INTO devices(id, device_code, name, dev_type, station_code) VALUES(?,?,?,'inverter','ST-01')`,
			id+1, code, code).Error; err != nil {
			t.Fatal(err)
		}
	}
	catalog := &fleet.Catalog{Stations: []fleet.Station{{
		Code: "ST-01", Scope: fleet.ScopeBaseline, ExpectedDevices: 3,
		Explicit: []string{"INV-1001", "INV-1002", "INV-1003"},
	}}}

	report, err := EnsureSimulationPolicyDevices(db, catalog)
	if err != nil {
		t.Fatal(err)
	}
	if report.Expected != 3 || report.Verified != 3 || len(report.Missing) != 0 || len(report.Mismatched) != 0 {
		t.Fatalf("unexpected report: %+v", report)
	}
	if report.OK {
		t.Fatal("report.OK must stay false when the catalog is not the expected 143 devices")
	}
	var rows int64
	if err := db.Table("simulation_alarm_policy_devices").Count(&rows).Error; err != nil || rows != 3 {
		t.Fatalf("policy rows = %d, err = %v", rows, err)
	}
	for id := int64(1); id <= 3; id++ {
		controlled, err := IsControlledSimulationDevice(db, id)
		if err != nil || !controlled {
			t.Fatalf("device %d controlled = %v, err = %v", id, controlled, err)
		}
	}
	if controlled, err := IsControlledSimulationDevice(db, 4); err != nil || controlled {
		t.Fatalf("device 4 controlled = %v, err = %v", controlled, err)
	}

	// 台账软删除后核对失败，但既有身份必须保持，不允许退回自然告警。
	if err := db.Exec(`UPDATE devices SET deleted_at = ? WHERE id = 3`, time.Now()).Error; err != nil {
		t.Fatal(err)
	}
	report, err = EnsureSimulationPolicyDevices(db, catalog)
	if err != nil {
		t.Fatal(err)
	}
	if report.Verified != 2 || len(report.Missing) != 1 {
		t.Fatalf("anomaly report = %+v", report)
	}
	if controlled, err := IsControlledSimulationDevice(db, 3); err != nil || !controlled {
		t.Fatalf("existing identity lost: %v, %v", controlled, err)
	}
}

func TestEnsureSimulationSlotsSkipsPastAndKeepsExisting(t *testing.T) {
	db := openSimulationTestDB(t)
	for index, station := range []string{"PV-01", "PV-02", "PV-03", "PV-04", "ST-01"} {
		deviceID := int64(index + 1)
		if err := db.Exec(`INSERT INTO simulation_alarm_policy_devices(
			station_code, device_code, device_id, source, active, verified_at, created_at, updated_at)
			VALUES(?,?,?,'fleet_catalog',TRUE,?,?,?)`,
			station, station+"-DEV-01", deviceID, time.Now(), time.Now(), time.Now()).Error; err != nil {
			t.Fatal(err)
		}
	}
	first := time.Date(2026, 10, 21, 8, 0, 0, 0, SimulationLocation())
	if err := EnsureSimulationSlots(db, time.Date(2026, 10, 21, 10, 30, 0, 0, SimulationLocation()), first); err != nil {
		t.Fatal(err)
	}
	type slotRow struct {
		SlotHour   int
		Status     string
		Reason     *string
		DeviceCode string
	}
	var rows []slotRow
	if err := db.Raw(`SELECT slot_hour, status, reason, device_code FROM simulation_alarm_slots ORDER BY slot_hour`).Scan(&rows).Error; err != nil {
		t.Fatal(err)
	}
	if len(rows) != 5 {
		t.Fatalf("created %d slots, want 5", len(rows))
	}
	if rows[0].Status != "skipped" || rows[0].Reason == nil || *rows[0].Reason != "window_missed" {
		t.Fatalf("09:00 slot = %+v", rows[0])
	}
	for _, row := range rows[1:] {
		if row.Status != "pending" || row.Reason != nil {
			t.Fatalf("future slot = %+v", row)
		}
	}
	// 同日重跑不新增、不重置已有状态；也不会补发已错过的 09:00。
	if err := EnsureSimulationSlots(db, time.Date(2026, 10, 21, 18, 0, 0, 0, SimulationLocation()), first); err != nil {
		t.Fatal(err)
	}
	var count int64
	if err := db.Table("simulation_alarm_slots").Count(&count).Error; err != nil || count != 5 {
		t.Fatalf("slots after rerun = %d, err = %v", count, err)
	}
	var firstStatus string
	if err := db.Raw(`SELECT status FROM simulation_alarm_slots WHERE slot_hour = 9`).Scan(&firstStatus).Error; err != nil || firstStatus != "skipped" {
		t.Fatalf("09:00 status after rerun = %q, err = %v", firstStatus, err)
	}
}

func TestSimulationFirstEnabledIsWrittenOnce(t *testing.T) {
	db := openSimulationTestDB(t)
	first := time.Date(2026, 10, 21, 1, 0, 0, 0, time.UTC)
	got, err := SimulationFirstEnabledAt(db, first)
	if err != nil {
		t.Fatal(err)
	}
	if !got.Equal(first) {
		t.Fatalf("first enabled = %s, want %s", got, first)
	}
	got, err = SimulationFirstEnabledAt(db, first.Add(2*time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	if !got.Equal(first) {
		t.Fatalf("first enabled was overwritten: %s", got)
	}
}
