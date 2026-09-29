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
	payload, err := json.Marshal(map[string]any{
		"event_id": eventKey, "category": category, "state": state,
		"alarm_id": alarm.ID, "device_code": device.DeviceCode,
		"station_code": device.StationCode, "metric": alarm.Metric,
		"level": alarm.Level, "value": alarm.Value,
		"threshold": alarm.Threshold, "occurred_at": occurredAt,
	})
	if err != nil {
		return err
	}
	return tx.Exec(`INSERT INTO business_notification_outbox(event_key,payload)
		VALUES(?,?::jsonb) ON CONFLICT(event_key) DO NOTHING`, eventKey, string(payload)).Error
}
