package api

import (
	"context"
	"encoding/json"
	"errors"
	"math"
	"net/http/httptest"
	"os"
	"reflect"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/gorilla/websocket"
	"github.com/redis/go-redis/v9"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"powersystem/backend/internal/model"
	"powersystem/backend/internal/service"
	"powersystem/backend/internal/telemetry"
)

func TestRealtimeAlarmsRetainHistoryAndCommitBeforeWebsocket(t *testing.T) {
	dsn := os.Getenv("ALARM_REPAIR_TEST_DSN")
	if dsn == "" {
		t.Skip("requires a dedicated alarm_repair_20261030_* database and loopback Redis")
	}
	db, err := gorm.Open(postgres.Open(dsn), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		t.Fatal("fixture database unavailable")
	}
	pool, _ := db.DB()
	defer pool.Close()
	var name string
	if db.Raw("SELECT current_database()").Scan(&name).Error != nil || !strings.HasPrefix(name, "alarm_repair_20261030_") {
		t.Fatal("isolated fixture required")
	}
	rd := redis.NewClient(&redis.Options{Addr: "127.0.0.1:6379", Password: os.Getenv("ALARM_REPAIR_REDIS_PASSWORD"), DB: 15})
	defer rd.Close()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	if err := rd.Ping(ctx).Err(); err != nil {
		t.Fatal("fixture Redis unavailable")
	}
	s := &Server{db: db, redis: rd, runContext: ctx, clients: make(map[*websocket.Conn]*realtimeClient)}
	s.ensureRealtime()
	defer func() { s.realtime.cancel(); <-s.realtime.done }()
	router := gin.New()
	router.GET("/ws/realtime", s.websocket)
	httpServer := httptest.NewServer(router)
	defer httpServer.Close()
	ticket, err := randomTicket()
	if err != nil {
		t.Fatal(err)
	}
	if err := rd.SetNX(ctx, "ws-ticket:"+ticket, "1", time.Minute).Err(); err != nil {
		t.Fatal(err)
	}
	defer rd.Del(ctx, "ws-ticket:"+ticket)
	ws, _, err := websocket.DefaultDialer.Dial("ws"+strings.TrimPrefix(httpServer.URL, "http")+"/ws/realtime?ticket="+ticket, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer ws.Close()
	device := model.Device{DeviceCode: "ALARM-REPAIR-REAL", Name: "isolated physical device", StationCode: "ALARM-REPAIR", DevType: "inverter"}
	must := func(err error) {
		t.Helper()
		if err != nil {
			t.Fatal(err)
		}
	}
	must(db.Create(&device).Error)
	must(db.Create(&model.AlarmRule{DeviceID: device.ID, Metric: "temperature", Operator: ">", Threshold: 80, Level: "urgent", Enabled: true, Version: "fixture"}).Error)
	makeItem := func(seq int64, stamp time.Time, temperature float64, confirmed bool) model.Inbox {
		t.Helper()
		sample := telemetry.Sample{SchemaVersion: 1, RunID: "alarm-repair-api", DeviceCode: device.DeviceCode, StationCode: device.StationCode, Seq: seq, TS: stamp.UnixMilli(), Voltage: 400, Current: 10, Power: math.Sqrt(3) * 400 * 10 * .99 / 1000, Temperature: temperature, Status: 1}
		payload, err := json.Marshal(sample)
		must(err)
		item := model.Inbox{DeviceID: device.ID, RunID: sample.RunID, Seq: seq, TS: sample.TS, Payload: payload, ReceivedAt: stamp}
		if confirmed {
			now := time.Now().UTC()
			item.TDConfirmedAt = &now
		}
		must(db.Create(&item).Error)
		return item
	}
	setSource := func(item model.Inbox) {
		must(db.Exec(`INSERT INTO telemetry_snapshots(device_id,inbox_id,ts_ms,confirmed_at) VALUES(?,?,?,now()) ON CONFLICT(device_id) DO UPDATE SET inbox_id=excluded.inbox_id,ts_ms=excluded.ts_ms,confirmed_at=excluded.confirmed_at`, device.ID, item.ID, item.TS).Error)
	}
	readEvent := func(want string) service.AlarmEvent {
		t.Helper()
		must(ws.SetReadDeadline(time.Now().Add(2 * time.Second)))
		var event service.AlarmEvent
		must(ws.ReadJSON(&event))
		if event.Type != want {
			t.Fatalf("expected %s, got %+v", want, event)
		}
		var persisted model.AlarmRecord
		must(db.First(&persisted, event.Data.ID).Error)
		if persisted.Status != event.Data.Status {
			t.Fatal("event preceded commit")
		}
		return event
	}
	head := makeItem(1, time.Now().UTC().Add(-time.Hour), 40, false)
	must(db.Model(&head).Updates(map[string]any{"business_state": "retry", "failure_code": "td_missing", "attempts": 9}).Error)
	current := makeItem(2, time.Now().UTC(), 82, true)
	setSource(current)
	// Inject an outbox failure. Alarm, watermark, marker and WS must all wait.
	must(db.Exec(`CREATE FUNCTION alarm_repair_fail() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'isolated outbox failure'; END $$`).Error)
	must(db.Exec(`CREATE TRIGGER alarm_repair_fail BEFORE INSERT ON business_notification_outbox FOR EACH ROW EXECUTE FUNCTION alarm_repair_fail()`).Error)
	err = s.evaluateRealtimeSource(ctx, device, current.ID)
	must(db.Exec(`DROP TRIGGER alarm_repair_fail ON business_notification_outbox; DROP FUNCTION alarm_repair_fail()`).Error)
	if err == nil {
		t.Fatal("expected outbox failure")
	}
	var count int64
	db.Model(&model.AlarmRecord{}).Where("device_id=?", device.ID).Count(&count)
	if count != 0 {
		t.Fatal("alarm committed without outbox")
	}
	db.Table("telemetry_alarm_progress").Where("device_id=?", device.ID).Count(&count)
	if count != 0 {
		t.Fatal("watermark advanced after rollback")
	}
	var failed model.Inbox
	must(db.First(&failed, current.ID).Error)
	if failed.AlarmEvaluation != nil {
		t.Fatal("marker committed after rollback")
	}
	must(db.Exec("UPDATE alarm_maintenance_state SET enabled=true WHERE id=1").Error)
	err = s.evaluateRealtimeSource(ctx, device, current.ID)
	must(db.Exec("UPDATE alarm_maintenance_state SET enabled=false WHERE id=1").Error)
	if !errors.Is(err, service.ErrAlarmMaintenance) {
		t.Fatal("maintenance ignored")
	}
	must(s.evaluateRealtimeSource(ctx, device, current.ID))
	created := readEvent("alarm_created")
	must(s.evaluateRealtimeSource(ctx, device, current.ID)) // retry must not duplicate
	must(s.evaluateRealtimeSource(ctx, device, head.ID))    // stale source cannot evaluate
	unconfirmed := makeItem(3, time.Now().UTC().Add(time.Millisecond), 82, false)
	setSource(unconfirmed)
	must(s.evaluateRealtimeSource(ctx, device, unconfirmed.ID))
	recovered := makeItem(4, time.Now().UTC().Add(2*time.Millisecond), 40, true)
	setSource(recovered)
	must(s.evaluateRealtimeSource(ctx, device, recovered.ID))
	recovery := readEvent("alarm_recovered")
	if recovery.Data.ID != created.Data.ID {
		t.Fatal("recovered a different alarm")
	}
	var retained model.Inbox
	must(db.First(&retained, head.ID).Error)
	var originalPayload, retainedPayload any
	must(json.Unmarshal(head.Payload, &originalPayload))
	must(json.Unmarshal(retained.Payload, &retainedPayload))
	if retained.ProcessedAt != nil || retained.FailureCode == nil || *retained.FailureCode != "td_missing" || retained.Attempts != 9 || !reflect.DeepEqual(originalPayload, retainedPayload) {
		t.Fatal("historical gap evidence changed")
	}
	retained = model.Inbox{}
	must(db.First(&retained, current.ID).Error)
	if retained.ProcessedAt != nil || retained.AlarmEvaluation == nil || *retained.AlarmEvaluation != "evaluated" {
		t.Fatal("current alarm mistaken for historical business completion")
	}
	db.Raw("SELECT count(*) FROM business_notification_outbox WHERE event_key IN (?,?)", "alarm:"+strconv.FormatInt(created.Data.ID, 10)+":triggered", "alarm:"+strconv.FormatInt(created.Data.ID, 10)+":recovered").Scan(&count)
	if count != 2 {
		t.Fatal("notification outbox not exactly once", count)
	}
	t.Log("real PG/Redis/WSS: current trigger/recovery bypass missing history; rollback, maintenance, TD confirmation, durable dedup and commit-before-event verified")
}
