package api

import (
	"context"
	"fmt"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/redis/go-redis/v9"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"

	"powersystem/backend/internal/fleet"
	"powersystem/backend/internal/model"
	"powersystem/backend/internal/service"
	"powersystem/backend/internal/store"
)

// Run only in the release driver's no-port, no-production-network fixture.
func TestV2PostgresRedisFixture(t *testing.T) {
	dsn := os.Getenv("INTERPRET_FIXTURE_DSN")
	if dsn == "" {
		t.Skip("isolated Postgres fixture only")
	}
	if !strings.Contains(dsn, "dbname=interpret_fixture") || !strings.Contains(dsn, "host=127.0.0.1") {
		t.Fatal("refusing non-fixture database")
	}
	db, err := gorm.Open(postgres.Open(dsn), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		t.Fatal(err)
	}
	var database string
	db.Raw("SELECT current_database()").Scan(&database)
	if database != "interpret_fixture" {
		t.Fatal("wrong database")
	}
	must := func(err error) {
		t.Helper()
		if err != nil {
			t.Fatal(err)
		}
	}
	count := func(table string) int64 { var n int64; must(db.Table(table).Count(&n).Error); return n }
	// Exercise the application's real statement splitter, not only psql.
	for _, path := range []string{"009_simulation_alarms.sql", "010_interpretation_v2.sql", "011_alarm_maintenance.sql"} {
		must(store.Migrate(db, "../../deploy/postgres/"+path))
	}
	catalog, err := fleet.Load("../../../01-data-chain/simulator/fleet.json")
	must(err)
	identities, err := service.SimulationCatalogIdentities(catalog)
	must(err)
	for _, id := range identities {
		must(db.Create(&model.Device{DeviceCode: id.DeviceCode, Name: "fixture", DevType: "inverter", StationCode: id.StationCode, GroupName: "fixture", IsSimulated: id.StationCode != "ST-01"}).Error)
	}
	real := model.Device{DeviceCode: "REAL-FIXTURE", Name: "fixture real", DevType: "inverter", StationCode: "REAL", GroupName: "fixture"}
	must(db.Create(&real).Error)
	report, err := service.EnsureSimulationPolicyDevices(db, catalog)
	must(err)
	if !report.OK || report.Verified != 143 {
		t.Fatal("identity verification failed")
	}
	controlled, err := service.IsControlledSimulationDevice(db, real.ID)
	must(err)
	if controlled {
		t.Fatal("real device suppressed")
	}
	day := time.Date(2026, 10, 22, 8, 0, 0, 0, service.SimulationLocation())
	must(service.EnsureSimulationSlots(db, day, day))
	var slots []service.SimulationSlot
	must(db.Order("scheduled_at").Find(&slots).Error)
	if len(slots) != 5 {
		t.Fatal("not five slots")
	}
	for _, slot := range slots {
		var group sync.WaitGroup
		var fired atomic.Int32
		for i := 0; i < 6; i++ {
			group.Add(1)
			go func(slot service.SimulationSlot) {
				defer group.Done()
				event, err := service.FireSimulationSlot(db, slot.ID, slot.ScheduledAt.Add(5*time.Second))
				if err != nil {
					t.Error(err)
				}
				if event != nil {
					fired.Add(1)
				}
			}(slot)
		}
		group.Wait()
		if fired.Load() != 1 {
			t.Fatal("duplicate concurrent event", fired.Load())
		}
	}
	if count("alarm_records") != 5 || count("business_notification_outbox") != 5 {
		t.Fatal("five event transactions failed")
	}
	events, err := service.CloseExpiredSimulationSlots(db, day.Add(12*time.Hour))
	must(err)
	if len(events) != 5 {
		t.Fatal("auto-end failed")
	}
	var ended int64
	must(db.Table("alarm_records").Where("close_reason='simulation_demo_ended'").Count(&ended).Error)
	if ended != 5 {
		t.Fatal("wrong ending reason")
	}
	must(service.EnsureSimulationSlots(db, day.Add(13*time.Hour), day))
	if count("simulation_alarm_slots") != 5 {
		t.Fatal("restart duplicated slots")
	}
	must(service.EnsureSimulationSlots(db, day.AddDate(0, 0, 1), day))
	if count("simulation_alarm_slots") != 10 {
		t.Fatal("daily boundary did not advance")
	}
	s := &Server{db: db}
	for id := int64(1); id <= 6; id++ {
		var device model.Device
		must(db.First(&device, id).Error)
		must(s.savePrediction(device, predictionResponse{DeviceID: id, WindowEndMS: time.Now().UnixMilli(), Probability: .94, Threshold: .35, RiskLevel: "high", ModelVersion: "fixture", TopFactors: []byte("[]"), Source: "synthetic-trained"}))
	}
	if count("prediction_records") != 6 || count("alarm_records") != 5 {
		t.Fatal("prediction either lost or created natural simulation alarm")
	}
	must(s.savePrediction(real, predictionResponse{DeviceID: real.ID, WindowEndMS: time.Now().UnixMilli(), Probability: .94, Threshold: .35, RiskLevel: "high", ModelVersion: "fixture", TopFactors: []byte("[]"), Source: "synthetic-trained"}))
	if count("alarm_records") != 6 {
		t.Fatal("real prediction suppressed")
	}
	must(db.Exec("INSERT INTO agent_monitor_events(event_key,level,occurred_at,payload) VALUES('monitor:fixture','major',now(),'{}')").Error)
	must(db.Exec("INSERT INTO agent_interpretations(event_key,category,level,occurred_at) VALUES('monitor:fixture','platform_monitor','major',now())").Error)
	must(db.Exec("INSERT INTO agent_interpretations(event_key,category,level,occurred_at,alarm_id,protocol_version,lease_until,lease_token,task_status) VALUES('alarm:fixture','device_alarm','minor',now(),1,2,now()+interval '60 seconds','current','running')").Error)
	var successes atomic.Int32
	var group sync.WaitGroup
	for i := 0; i < 8; i++ {
		group.Add(1)
		go func() {
			defer group.Done()
			r := db.Exec("UPDATE agent_interpretations SET model_calls=model_calls+1 WHERE event_key='alarm:fixture' AND lease_token='current' AND model_calls<2")
			if r.Error != nil {
				t.Error(r.Error)
			}
			if r.RowsAffected == 1 {
				successes.Add(1)
			}
		}()
	}
	group.Wait()
	if successes.Load() != 2 {
		t.Fatal("persistent call budget exceeded")
	}
	if db.Exec("UPDATE agent_interpretations SET reason='stale' WHERE event_key='alarm:fixture' AND lease_token='old'").RowsAffected != 0 {
		t.Fatal("old lease overwrote result")
	}
	client := redis.NewClient(&redis.Options{Addr: "127.0.0.1:6379"})
	defer client.Close()
	ctx := context.Background()
	now := time.Now().UnixMilli()
	for i := 0; i < 3; i++ {
		permit, err := circuitPermit.Run(ctx, client, []string{circuitKey, circuitKey + ":half"}, now, "normal").Int64Slice()
		must(err)
		if permit[0] != 1 {
			t.Fatal("early circuit open")
		}
		_, err = circuitFinish.Run(ctx, client, []string{circuitKey, circuitKey + ":half"}, permit[2], 0, now, 0, permit[3], "normal").Result()
		must(err)
	}
	permit, err := circuitPermit.Run(ctx, client, []string{circuitKey, circuitKey + ":half"}, now+30000, "blocked").Int64Slice()
	must(err)
	if permit[0] != 0 {
		t.Fatal("open circuit called supplier")
	}
	half, err := circuitPermit.Run(ctx, client, []string{circuitKey, circuitKey + ":half"}, now+61000, "half").Int64Slice()
	must(err)
	if half[0] != 1 || half[3] != 1 {
		t.Fatal("half-open not admitted")
	}
	concurrent, err := circuitPermit.Run(ctx, client, []string{circuitKey, circuitKey + ":half"}, now+61000, "other").Int64Slice()
	must(err)
	if concurrent[0] != 0 {
		t.Fatal("multiple half-open probes")
	}
	_, err = circuitFinish.Run(ctx, client, []string{circuitKey, circuitKey + ":half"}, half[2], 1, now+61000, 0, half[3], "half").Result()
	must(err)
	recovered, err := circuitPermit.Run(ctx, client, []string{circuitKey, circuitKey + ":half"}, now+62000, "new").Int64Slice()
	must(err)
	if recovered[0] != 1 || recovered[3] != 0 {
		t.Fatal("circuit did not recover")
	}
	var cutoff int64
	must(db.Table("alarm_records").Select("max(id)").Scan(&cutoff).Error)
	script, err := os.ReadFile("../../scripts/reset-business-alarms.sql")
	must(err)
	sql := strings.ReplaceAll(string(script), "\\set ON_ERROR_STOP on", "")
	sql = strings.ReplaceAll(sql, ":'cutoff'", "'"+fmt.Sprint(cutoff)+"'")
	must(db.Exec("UPDATE alarm_maintenance_state SET enabled=TRUE WHERE id=1").Error)
	must(db.Exec(sql).Error)
	if count("alarm_records") != 0 || count("business_notification_outbox") != 0 || count("agent_monitor_events") != 1 || count("prediction_records") != 7 || count("devices") != 144 {
		t.Fatal("reset violated preservation boundary")
	}
	if count("agent_interpretations") != 1 {
		t.Fatal("monitor interpretation deleted")
	}
	fresh := model.AlarmRecord{DeviceID: real.ID, Metric: "fixture_after_reset", Level: "minor", Value: 1, Threshold: 0, Status: "unhandled", TriggeredAt: time.Now()}
	must(db.Create(&fresh).Error)
	if fresh.ID <= cutoff {
		t.Fatal("alarm identity sequence reset")
	}
}
