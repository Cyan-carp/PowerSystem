package api

import (
	"context"
	csvpkg "encoding/csv"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"reflect"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"go.uber.org/zap"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"powersystem/backend/internal/config"
	"powersystem/backend/internal/model"
	"powersystem/backend/internal/tdengine"
	"powersystem/backend/internal/telemetry"
)

func TestHourlyFoldExactBoundariesAndGaps(t *testing.T) {
	start := int64(1793300400000) / availabilityHour * availabilityHour
	end := start + 3*availabilityHour
	samples := []samplePoint{{start - 10_000, 1}, {start + 5_000, 0}, {start + availabilityHour - 5_000, 2}, {start + availabilityHour + 30_000, 1}, {end - 10_000, 0}}
	fold := newHourlyFold(start, end)
	for _, p := range samples {
		fold.add(p)
	}
	got := fold.finish()
	for h := start; h < end; h += availabilityHour {
		want := computeAvailability(append([]samplePoint{}, samples...), h, h+availabilityHour)
		row := got[h]
		if row.CoveredMs != want.CoveredMs || row.RunningMs != want.RunningMs || row.StoppedMs != want.StoppedMs || row.FaultMs != want.FaultMs || row.UnknownMs != want.UnknownMs {
			t.Fatalf("hour %d: %+v want %+v", h, row, want)
		}
	}
}

// This integration suite refuses any DSN except its disposable loopback fixture.
func TestAvailabilityRollupPostgres(t *testing.T) {
	dsn := os.Getenv("REPORT_ROLLUP_TEST_DSN")
	if dsn == "" {
		t.Skip("dedicated disposable PostgreSQL required")
	}
	u, err := url.Parse(dsn)
	if err != nil || u.Hostname() != "127.0.0.1" || u.Port() != "15447" || u.Path != "/availability_report_test" {
		t.Fatal("isolated rollup test database required")
	}
	db, err := gorm.Open(postgres.Open(dsn), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		t.Fatal(err)
	}
	pool, _ := db.DB()
	defer pool.Close()
	pool.SetMaxOpenConns(12)
	cleanup := func() {
		db.Exec("DELETE FROM energy_dirty_days WHERE device_id IN (SELECT id FROM devices WHERE device_code LIKE 'ROLLUP-%')")
		db.Exec("DELETE FROM telemetry_inbox WHERE device_id IN (SELECT id FROM devices WHERE device_code LIKE 'ROLLUP-%')")
		db.Exec("DELETE FROM availability_hourly WHERE device_id IN (SELECT id FROM devices WHERE device_code LIKE 'ROLLUP-%')")
		db.Exec("DELETE FROM devices WHERE device_code LIKE 'ROLLUP-%'")
	}
	cleanup()
	t.Cleanup(cleanup)
	// Migrations are applied twice through cmd/api -migrate-only before this suite.
	var installed bool
	if err = db.Raw("SELECT to_regclass('availability_hourly') IS NOT NULL").Scan(&installed).Error; err != nil || !installed {
		t.Fatal("actual application migration entry must run first", err)
	}
	start := time.Now().UTC().Truncate(time.Hour).Add(-4 * time.Hour)
	end := start.Add(3*time.Hour + 53*time.Minute)
	var devices []model.Device
	for i := 0; i < 2; i++ {
		d := model.Device{DeviceCode: fmt.Sprintf("ROLLUP-%d", i), Name: "fixture", DevType: "inverter", StationCode: "ROLLUP", CreatedAt: start.Add(-time.Hour)}
		if err = db.Create(&d).Error; err != nil {
			t.Fatal(err)
		}
		devices = append(devices, d)
	}
	var mu sync.RWMutex
	points := map[int64][]samplePoint{}
	for _, d := range devices {
		for ts := start.Add(-20 * time.Second).UnixMilli(); ts < end.UnixMilli(); ts += 5000 {
			// Includes a long missing-data window and thousands of samples per page.
			if ts > start.Add(time.Hour+10*time.Minute).UnixMilli() && ts < start.Add(time.Hour+12*time.Minute).UnixMilli() {
				continue
			}
			points[d.ID] = append(points[d.ID], samplePoint{ts, int((ts / 5000) % 3)})
		}
	}
	var onRead func()
	var callbackMu sync.Mutex
	fake := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		q := string(raw)
		rows := [][]any{}
		if strings.HasPrefix(q, "SELECT ts,status") {
			callbackMu.Lock()
			callback := onRead
			onRead = nil
			callbackMu.Unlock()
			if callback != nil {
				callback()
			}
			mu.RLock()
			defer mu.RUnlock()
			var id, at, until int64
			var limit int
			if strings.Contains(q, "WHERE ts <=") {
				fmt.Sscanf(q, "SELECT ts,status FROM rollup.t_device_%d WHERE ts <= %d", &id, &at)
				for _, p := range points[id] {
					if p.TS <= at {
						rows = [][]any{{p.TS, p.Status}}
					}
				}
			} else {
				fmt.Sscanf(q, "SELECT ts,status FROM rollup.t_device_%d WHERE ts > %d AND ts < %d ORDER BY ts ASC LIMIT %d", &id, &at, &until, &limit)
				for _, p := range points[id] {
					if p.TS > at && p.TS < until && len(rows) < limit {
						rows = append(rows, []any{p.TS, p.Status})
					}
				}
			}
		}
		json.NewEncoder(w).Encode(map[string]any{"code": 0, "data": rows})
	}))
	defer fake.Close()
	s := &Server{db: db, td: tdengine.New(config.Config{TDURL: fake.URL, TDDatabase: "rollup"}), rollupInstalled: true}
	ctx := context.Background()
	check := func(wantState string) {
		t.Helper()
		want, e := s.availabilityDevices(ctx, devices, start.Add(7*time.Minute), end)
		if e != nil {
			t.Fatal(e)
		}
		got, meta, e := s.availabilityReportDevices(ctx, devices, start.Add(7*time.Minute), end)
		if e != nil {
			t.Fatal(e)
		}
		if !reflect.DeepEqual(got, want) {
			t.Fatalf("rollup differs from raw oracle: %+v vs %+v", got, want)
		}
		if meta.State != wantState {
			t.Fatalf("state=%s want %s", meta.State, wantState)
		}
	}
	if err = s.SetAvailabilityRollupRead(ctx, true); err != nil {
		t.Fatal(err)
	}
	check("degraded")
	if err = s.AvailabilityRollupStep(ctx, 20*time.Second); err != nil {
		t.Fatal(err)
	}
	check("ready")
	// Exercise the real single-statement MQTT admission path with the new CTE.
	acks := make(chan int, 1)
	s.log = zap.NewNop()
	payload, _ := json.Marshal(telemetry.Sample{SchemaVersion: 1, RunID: "rollup-test", DeviceCode: devices[0].DeviceCode, StationCode: devices[0].StationCode, Seq: 1, TS: start.Add(time.Hour).UnixMilli(), Voltage: 400, Current: 1, Temperature: 25, Power: 1, Status: 1})
	if !s.receive(nil, ackOrderMessage{payload: payload, acks: acks}) {
		t.Fatal("admission failed with rollup installed")
	}
	if len(acks) != 1 {
		t.Fatal("admission not acknowledged")
	}
	check("warming")
	if err = s.AvailabilityRollupStep(ctx, 20*time.Second); err != nil {
		t.Fatal(err)
	}
	check("ready")
	// Re-entering the worker is idempotent and a fresh process uses the same rows.
	if err = s.AvailabilityRollupStep(ctx, 20*time.Second); err != nil {
		t.Fatal(err)
	}
	restarted := &Server{db: db, td: s.td, rollupInstalled: true}
	s = restarted
	check("ready")
	late := start.Add(time.Hour + 10*time.Minute + 5*time.Second).UnixMilli()
	mu.Lock()
	points[devices[0].ID] = append(points[devices[0].ID], samplePoint{late, 2})
	// Keep fake TD ordering identical to the real timestamp primary key.
	sort.Slice(points[devices[0].ID], func(i, j int) bool { return points[devices[0].ID][i].TS < points[devices[0].ID][j].TS })
	mu.Unlock()
	if err = dirtyAvailability(db, devices[0].ID, late); err != nil {
		t.Fatal(err)
	}
	check("warming")
	// Change its generation while rebuilding. That scan must not clear the marker.
	callbackMu.Lock()
	onRead = func() {
		if e := dirtyAvailability(db, devices[0].ID, late); e != nil {
			t.Error(e)
		}
	}
	callbackMu.Unlock()
	if err = s.AvailabilityRollupStep(ctx, 20*time.Second); err != nil {
		t.Fatal(err)
	}
	check("warming")
	if err = s.AvailabilityRollupStep(ctx, 20*time.Second); err != nil {
		t.Fatal(err)
	}
	check("ready")
	// JSON and CSV share the query, metadata and duration totals.
	router := gin.New()
	router.GET("/report", s.availability)
	query := "?start=" + url.QueryEscape(start.Add(7*time.Minute).Format(time.RFC3339Nano)) + "&end=" + url.QueryEscape(end.Format(time.RFC3339Nano)) + "&station_code=ROLLUP"
	response := httptest.NewRecorder()
	router.ServeHTTP(response, httptest.NewRequest("GET", "/report"+query, nil))
	if response.Code != 200 {
		t.Fatal(response.Body.String())
	}
	csv := httptest.NewRecorder()
	router.ServeHTTP(csv, httptest.NewRequest("GET", "/report"+query+"&format=csv", nil))
	if csv.Code != 200 || csv.Header().Get("X-Aggregation-State") != "ready" || !strings.Contains(csv.Body.String(), "availability-v1") {
		t.Fatal("CSV metadata/results missing", csv.Body.String())
	}
	var envelope struct {
		Data struct{ Devices []availabilityResult }
	}
	if err = json.Unmarshal(response.Body.Bytes(), &envelope); err != nil {
		t.Fatal(err)
	}
	parsed, err := csvpkg.NewReader(strings.NewReader(strings.TrimPrefix(csv.Body.String(), "\ufeff"))).ReadAll()
	if err != nil {
		t.Fatal(err)
	}
	for _, d := range envelope.Data.Devices {
		found := false
		for _, row := range parsed[1:] {
			if row[0] == "device" && row[1] == d.DeviceCode {
				found = true
				if row[8] != fmt.Sprint(d.CoveredMs) || row[7] != fmt.Sprint(d.UnknownMs) {
					t.Fatal("CSV differs from JSON", row, d)
				}
			}
		}
		if !found {
			t.Fatal("CSV lost device", d.DeviceCode)
		}
	}
	// The actual snapshot-confirmation CTE is exercised by PostgreSQL, not a mock.
	var count int
	cteQuery := fmt.Sprintf("WITH confirmed AS(SELECT %d::bigint device_id), %s source AS(SELECT 1) SELECT count(*) FROM confirmed", devices[0].ID, availabilityDirtyCTE(devices[0].ID, late))
	if err = db.Raw(cteQuery).Scan(&count).Error; err != nil || count != 1 {
		t.Fatal("snapshot dirty CTE", err)
	}
	check("warming")
	if err = db.Exec("UPDATE availability_rollup_state SET swept_at=now()-interval '2 minutes'").Error; err != nil {
		t.Fatal(err)
	}
	check("degraded")
	if err = s.SetAvailabilityRollupRead(ctx, false); err != nil {
		t.Fatal(err)
	}
	check("warming")
}
