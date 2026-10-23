package service

import (
	"encoding/json"
	"fmt"

	"gorm.io/gorm"

	"powersystem/backend/internal/model"
)

// QueueAlarmNotification shares the caller's transaction with the business
// state change. The unique event key prevents repeated telemetry or retries
// from creating duplicate deliveries.
func QueueAlarmNotification(tx *gorm.DB, device model.Device, alarm model.AlarmRecord, state string) error {
	if alarm.Metric == SimulationMetric && alarm.ScenarioID != nil {
		var slot SimulationSlot
		if err := tx.First(&slot, *alarm.ScenarioID).Error; err != nil {
			return err
		}
		return QueueSimulationAlarmNotification(tx, device, alarm, slot, state)
	}
	return queueAlarmNotification(tx, device, alarm, state, nil)
}

// QueueSimulationAlarmNotification 保持 device_alarm 类别和既有字段，
// 只增加模拟演示标记与日程元数据，便于通知侧明确标注而不是冒充自然告警。
func QueueSimulationAlarmNotification(tx *gorm.DB, device model.Device, alarm model.AlarmRecord, slot SimulationSlot, state string) error {
	extra := map[string]any{
		"origin":                SimulationOrigin,
		"simulation":            true,
		"simulation_label":      "模拟演示",
		"scenario_id":           slot.ID,
		"scenario_date":         slot.SlotDate.Format("2006-01-02"),
		"scenario_hour":         slot.SlotHour,
		"scenario_station_code": slot.StationCode,
		"scenario_device_code":  slot.DeviceCode,
	}
	return queueAlarmNotification(tx, device, alarm, state, extra)
}

func queueAlarmNotification(tx *gorm.DB, device model.Device, alarm model.AlarmRecord, state string, extra map[string]any) error {
	if state != "triggered" && state != "acknowledged" && state != "recovered" && state != "closed" {
		return fmt.Errorf("unsupported notification state %q", state)
	}
	eventKey := fmt.Sprintf("alarm:%d:%s", alarm.ID, state)
	category := "device_alarm"
	if alarm.Metric == "ai_failure_risk" {
		category = "prediction_risk"
	}
	occurredAt := alarm.TriggeredAt
	if state == "acknowledged" && alarm.AckedAt != nil {
		occurredAt = *alarm.AckedAt
	}
	if (state == "recovered" || state == "closed") && alarm.RecoveredAt != nil {
		occurredAt = *alarm.RecoveredAt
	}
	payload := map[string]any{
		"event_id": eventKey, "category": category, "state": state,
		"alarm_id": alarm.ID, "device_code": device.DeviceCode,
		"station_code": device.StationCode, "metric": alarm.Metric,
		"level": alarm.Level, "value": alarm.Value,
		"threshold": alarm.Threshold, "occurred_at": occurredAt,
	}
	for key, value := range extra {
		payload[key] = value
	}
	encoded, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	return tx.Exec(`INSERT INTO business_notification_outbox(event_key,payload)
		VALUES(?,?::jsonb) ON CONFLICT(event_key) DO NOTHING`, eventKey, string(encoded)).Error
}
