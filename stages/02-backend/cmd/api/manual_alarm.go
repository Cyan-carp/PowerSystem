package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"strings"

	"gorm.io/gorm"
	"powersystem/backend/internal/config"
	"powersystem/backend/internal/model"
	"powersystem/backend/internal/service"
	"powersystem/backend/internal/store"
)

func runManualDemo(ctx context.Context, cfg config.Config, db *gorm.DB, mode, key, device, level string, id int64, actor, analysis string) error {
	var alarm model.AlarmRecord
	changed := false
	var err error
	switch mode {
	case "create":
		alarm, changed, err = service.CreateManualDemo(db, device, key, level, analysis)
	case "ack":
		alarm, changed, err = service.AckManualAPIDemo(db, id, actor)
	case "recover":
		alarm, changed, err = service.EndManualAPIDemo(db, id)
	case "status":
		err = db.First(&alarm, id).Error
		if err == nil && !service.IsManualDemo(alarm) {
			err = errors.New("not_a_manual_api_demo")
		}
	default:
		return errors.New("unknown_demo_operation")
	}
	if err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return errors.New("demo_device_or_alarm_not_found")
		}
		if strings.HasPrefix(err.Error(), "demo_") || strings.HasPrefix(err.Error(), "invalid_demo_") || err.Error() == "not_a_manual_api_demo" || errors.Is(err, service.ErrAlarmMaintenance) || errors.Is(err, service.ErrAlarmAckConflict) {
			return err
		}
		return errors.New("manual_demo_database_operation_failed")
	}
	var task struct {
		ID         int64           `json:"id"`
		TaskStatus string          `json:"task_status"`
		ModelCalls int             `json:"model_calls"`
		Reason     string          `json:"reason"`
		Result     json.RawMessage `json:"result"`
	}
	if err = db.Raw("SELECT id,task_status,model_calls,reason,result FROM agent_interpretations WHERE alarm_id=? ORDER BY id DESC LIMIT 1", alarm.ID).Scan(&task).Error; err != nil {
		return errors.New("demo_status_unavailable")
	}
	actualMode := "fixed"
	if service.IsManualAPIDemo(alarm) {
		actualMode = "model"
	}
	out := map[string]any{"operation": mode, "alarm_id": alarm.ID, "device_id": alarm.DeviceID, "status": alarm.Status, "changed": changed, "interpretation_mode": actualMode, "interpretation": task, "detail_path": fmt.Sprintf("/alarms?id=%d", alarm.ID)}
	if mode != "status" {
		kind := map[string]string{"create": "alarm_created", "ack": "alarm_acked", "recover": "alarm_recovered"}[mode]
		// Republish only the alarm's current valid state on an idempotent retry.
		if alarm.Status == "acked" {
			kind = "alarm_acked"
		}
		if alarm.Status == "recovered" {
			kind = "alarm_recovered"
		}
		rd, e := store.RedisContext(ctx, cfg)
		if e == nil {
			defer rd.Close()
			raw, _ := json.Marshal(map[string]any{"alarm_id": alarm.ID, "type": kind})
			count, e := rd.Publish(ctx, service.ManualAlarmEventsChannel, string(raw)).Result()
			out["live_subscribers"] = count
			out["live_event_published"] = e == nil
		} else {
			out["live_event_published"] = false
		}
	}
	return json.NewEncoder(os.Stdout).Encode(out)
}
