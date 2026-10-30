package api

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/gorilla/websocket"
	"github.com/redis/go-redis/v9"
	"go.uber.org/zap"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"powersystem/backend/internal/config"
	"powersystem/backend/internal/model"
	"powersystem/backend/internal/service"
)

func TestFixedAutomaticAndManualAPIChannel(t *testing.T) {
	dsn := os.Getenv("ALARM_FIXED_TEST_DSN")
	if dsn == "" {
		t.Skip("dedicated fixed-interpretation database/Redis fixture required")
	}
	db, e := gorm.Open(postgres.Open(dsn), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if e != nil {
		t.Fatal("fixture database unavailable")
	}
	pool, _ := db.DB()
	defer pool.Close()
	must := func(e error) {
		t.Helper()
		if e != nil {
			t.Fatal(e)
		}
	}
	var name string
	must(db.Raw("SELECT current_database()").Scan(&name).Error)
	if name != "alarm_repair_20261030_fixed" {
		t.Fatal("refusing non-fixture database")
	}
	rd := redis.NewClient(&redis.Options{Addr: "127.0.0.1:6379", Password: os.Getenv("ALARM_REPAIR_REDIS_PASSWORD"), DB: 14, ContextTimeoutEnabled: true})
	defer rd.Close()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	must(rd.Ping(ctx).Err())
	keys := []string{circuitKey, circuitKey + ":half"}
	n, e := rd.Exists(ctx, keys...).Result()
	must(e)
	if n != 0 {
		t.Fatal("fixture circuit keys already in use")
	}
	defer rd.Del(context.Background(), keys...)
	num, e := rd.PubSubNumSub(ctx, service.ManualAlarmEventsChannel).Result()
	must(e)
	if num[service.ManualAlarmEventsChannel] != 0 {
		t.Fatal("local demo event channel already in use")
	}
	var calls atomic.Int32
	provider := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		var request map[string]any
		must(json.NewDecoder(r.Body).Decode(&request))
		if r.URL.Path != "/internal/agent/interpret/v2" || request["event_origin"] != "simulation_demo" {
			t.Error("wrong model request route or origin")
		}
		facts := request["facts"].([]any)
		ref := facts[0].(map[string]any)["fact_id"].(string)
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]any{"protocol_version": 2, "event_key": request["event_key"], "evidence_hash": request["evidence_hash"], "status": "answered", "analysis_status": "answered", "model": "isolated-model-fixture", "analysis": map[string]any{"findings": []any{map[string]any{"template": "已取得演示事实：{{" + ref + "}}。", "fact_refs": []string{ref}}}, "hypotheses": []any{}, "suggestions": []any{}, "limitations": []any{}}})
	}))
	defer provider.Close()
	s := &Server{db: db, redis: rd, cfg: config.Config{AgentEnabled: true, AgentInterpretEnabled: true, AgentInterpretProtocol: "v2", AgentURL: provider.URL, AgentToken: "isolated-only"}, log: zap.NewNop(), runContext: ctx, clients: make(map[*websocket.Conn]*realtimeClient)}
	s.ensureRealtime()
	defer func() { s.realtime.cancel(); <-s.realtime.done }()
	claim := func(id int64) interpretationTask {
		t.Helper()
		var item interpretationTask
		must(db.Raw(`UPDATE agent_interpretations SET task_status='running',lease_token='fixed-fixture',lease_until=now()+interval '60 seconds',attempts=attempts+1 WHERE alarm_id=? OR (alarm_id IS NULL AND id=?) RETURNING *`, id, -id).Scan(&item).Error)
		if item.ID == 0 {
			t.Fatal("task not claimed")
		}
		return item
	}
	// Initialize the real scanner before creating new regular alarms.
	must(s.scanInterpretations(ctx))
	var device model.Device
	must(db.Where("device_code='INV-1001'").First(&device).Error)
	var firstRegular model.AlarmRecord
	for index, code := range []string{"PV-01-INV-20", "PV-02-INV-36", "PV-03-INV-20", "PV-04-INV-16", "INV-1002"} {
		var current model.Device
		must(db.Where("device_code=?", code).First(&current).Error)
		metric := "temperature"
		value, threshold := 82.0, 80.0
		if index == 1 {
			metric = "voltage"
			value = 445
			threshold = 430
		}
		a := model.AlarmRecord{DeviceID: current.ID, Metric: metric, Level: "major", Value: value, Threshold: threshold, Origin: "normal", Status: "unhandled", RuleVersion: "fixture", TriggeredAt: time.Now().UTC()}
		must(db.Create(&a).Error)
		if index == 0 {
			firstRegular = a
		}
		must(s.scanInterpretations(ctx))
		item := claim(a.ID)
		s.processInterpretation(ctx, item)
		var row struct {
			TaskStatus string
			ModelCalls int
			Result     json.RawMessage
		}
		must(db.Table("agent_interpretations").Where("id=?", item.ID).Take(&row).Error)
		var result map[string]any
		must(json.Unmarshal(row.Result, &result))
		if row.TaskStatus != "completed" || row.ModelCalls != 0 || result["interpretation_mode"] != "fixed" || len(result["facts"].([]any)) == 0 {
			t.Fatalf("regular result not fixed: %+v", row)
		}
		// A stale lease cannot overwrite this result or spend model tokens.
		item.LeaseToken = "obsolete"
		s.processInterpretation(ctx, item)
	}
	// Monitoring uses the same zero-model default too.
	must(db.Exec(`INSERT INTO agent_monitor_events(event_key,level,occurred_at,payload) VALUES('fixed-monitor','major',now(),'{}'::jsonb); INSERT INTO agent_interpretations(event_key,category,level,occurred_at,protocol_version) VALUES('fixed-monitor','platform_monitor','major',now(),2)`).Error)
	var monitor struct{ ID int64 }
	must(db.Raw("SELECT id FROM agent_interpretations WHERE event_key='fixed-monitor'").Scan(&monitor).Error)
	s.processInterpretation(ctx, claim(-monitor.ID))
	if calls.Load() != 0 {
		t.Fatal("automatic interpretation contacted model")
	}
	// Connect the real local WebSocket and actual Redis event receiver.
	router := gin.New()
	router.GET("/ws/realtime", s.websocket)
	web := httptest.NewServer(router)
	defer web.Close()
	ticket, e := randomTicket()
	must(e)
	must(rd.Set(ctx, "ws-ticket:"+ticket, "1", time.Minute).Err())
	defer rd.Del(ctx, "ws-ticket:"+ticket)
	ws, _, e := websocket.DefaultDialer.Dial("ws"+strings.TrimPrefix(web.URL, "http")+"/ws/realtime?ticket="+ticket, nil)
	must(e)
	defer ws.Close()
	done := make(chan struct{})
	go func() { defer close(done); s.manualAlarmEvents(ctx) }()
	defer func() {
		cancel()
		select {
		case <-done:
		case <-time.After(2 * time.Second):
			t.Error("event receiver failed to stop")
		}
	}()
	for deadline := time.Now().Add(2 * time.Second); ; {
		n, e := rd.PubSubNumSub(ctx, service.ManualAlarmEventsChannel).Result()
		must(e)
		if n[service.ManualAlarmEventsChannel] == 1 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("receiver did not subscribe")
		}
		time.Sleep(10 * time.Millisecond)
	}
	demo, _, e := service.CreateManualAPIDemo(db, device.DeviceCode, "api-channel-fixture", "urgent")
	must(e)
	publish := func(kind string, id int64) {
		raw, _ := json.Marshal(map[string]any{"type": kind, "alarm_id": id, "data": "untrusted body must be ignored"})
		must(rd.Publish(ctx, service.ManualAlarmEventsChannel, string(raw)).Err())
	}
	read := func(kind string) {
		t.Helper()
		must(ws.SetReadDeadline(time.Now().Add(2 * time.Second)))
		for {
			var event service.AlarmEvent
			must(ws.ReadJSON(&event))
			if event.Type == "interpretation_updated" {
				continue
			}
			if event.Type != kind || event.Data.ID != demo.ID {
				t.Fatalf("unexpected lifecycle event: %+v", event)
			}
			break
		}
	}
	publish("alarm_created", demo.ID)
	read("alarm_created")
	// Prepared, typed synthetic evidence tests the real v2 HTTP call/validation;
	// no vendor is contacted by this local correctness fixture.
	evidence := []eventEvidence{{ID: "E1", Tool: "get_alarm_detail", Status: "ok", Source: "fixture", CollectedAt: time.Now().UTC().Format(time.RFC3339Nano), Data: map[string]any{"metric": service.SimulationMetric, "value": 1.0, "threshold": 0.0, "origin": "simulation_demo", "scenario_label": "手动演示"}}}
	raw, _ := json.Marshal(evidence)
	must(db.Exec("UPDATE agent_interpretations SET evidence=?::jsonb WHERE alarm_id=?", string(raw), demo.ID).Error)
	item := claim(demo.ID)
	s.processInterpretation(ctx, item)
	var result struct {
		TaskStatus string
		ModelCalls int
		Result     json.RawMessage
	}
	must(db.Table("agent_interpretations").Where("id=?", item.ID).Take(&result).Error)
	if result.TaskStatus != "completed" || result.ModelCalls != 1 || calls.Load() != 1 || !strings.Contains(string(result.Result), "isolated-model-fixture") {
		t.Fatalf("manual real HTTP path failed: %+v calls=%d", result, calls.Load())
	}
	must(s.scanInterpretations(ctx))
	var duplicates int64
	must(db.Table("agent_interpretations").Where("alarm_id=?", demo.ID).Count(&duplicates).Error)
	if duplicates != 1 {
		t.Fatal("scanner duplicated manual task")
	}
	// An ID hint for a regular alarm is not accepted as a manual event.
	s.receiveManualAlarmEvent(ctx, []byte(fmt.Sprintf(`{"alarm_id":%d,"type":"alarm_created"}`, firstRegular.ID)))
	var actor model.User
	must(db.Where("username='demo-fixture-operator'").First(&actor).Error)
	_, _, e = service.AckManualAPIDemo(db, demo.ID, actor.Username)
	must(e)
	publish("alarm_acked", demo.ID)
	read("alarm_acked")
	_, _, e = service.EndManualAPIDemo(db, demo.ID)
	must(e)
	publish("alarm_recovered", demo.ID)
	read("alarm_recovered")
	if calls.Load() != 1 {
		t.Fatal("status changes caused another model request")
	}
	// A manual fixed demo uses the same create/recovery channel, without HTTP.
	demo, _, e = service.CreateManualDemo(db, device.DeviceCode, "fixed-channel-fixture", "minor", "fixed")
	must(e)
	publish("alarm_created", demo.ID)
	read("alarm_created")
	fixed := claim(demo.ID)
	s.processInterpretation(ctx, fixed)
	var fixedRow struct {
		TaskStatus string
		ModelCalls int
		Result     json.RawMessage
	}
	must(db.Table("agent_interpretations").Where("id=?", fixed.ID).Take(&fixedRow).Error)
	if fixedRow.TaskStatus != "completed" || fixedRow.ModelCalls != 0 || calls.Load() != 1 || !strings.Contains(string(fixedRow.Result), `"interpretation_mode": "fixed"`) {
		t.Fatalf("manual fixed mode spent model budget: %+v", fixedRow)
	}
	_, _, e = service.EndManualAPIDemo(db, demo.ID)
	must(e)
	publish("alarm_recovered", demo.ID)
	read("alarm_recovered")
	t.Log("five automatic alarms and monitor use zero HTTP/model reservations; manual demo uses real v2 HTTP validation once; committed create/ack/recover traverse real Redis and WebSocket; scanner and notification lifecycle remain shared")
}
