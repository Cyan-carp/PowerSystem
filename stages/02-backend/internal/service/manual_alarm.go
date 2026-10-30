package service

import (
	"errors"
	"fmt"
	"regexp"
	"time"

	"gorm.io/gorm"
	"gorm.io/gorm/clause"
	"powersystem/backend/internal/model"
)

const ManualAPIDemoVersion = "manual-api-demo/v1"
const ManualFixedDemoVersion = "manual-fixed-demo/v1"
const ManualAPIDemoEventPrefix = "manual-demo:"
const ManualAlarmEventsChannel = "powersystem:manual-alarm-events:v1"

var demoRequestID = regexp.MustCompile(`^[a-zA-Z0-9_-][a-zA-Z0-9_-]{7,63}$`)

func IsManualAPIDemo(a model.AlarmRecord) bool {
	return IsManualDemo(a) && a.RuleVersion == ManualAPIDemoVersion
}

func IsManualDemo(a model.AlarmRecord) bool {
	return a.Origin == SimulationOrigin && a.Metric == SimulationMetric && (a.RuleVersion == ManualAPIDemoVersion || a.RuleVersion == ManualFixedDemoVersion) && a.ScenarioID == nil
}

// Local operation: no telemetry, rule, daily-slot or scanner-cursor mutation.
func CreateManualAPIDemo(db *gorm.DB, code, requestID, level string) (model.AlarmRecord, bool, error) {
	return CreateManualDemo(db, code, requestID, level, "model")
}

func CreateManualDemo(db *gorm.DB, code, requestID, level, analysis string) (model.AlarmRecord, bool, error) {
	var alarm model.AlarmRecord
	created := false
	if !demoRequestID.MatchString(requestID) || code == "" || (level != "urgent" && level != "major" && level != "minor") || (analysis != "fixed" && analysis != "model") {
		return alarm, false, errors.New("invalid_demo_arguments")
	}
	err := TelemetryTransaction(db, func(tx *gorm.DB) error {
		key := ManualAPIDemoEventPrefix + requestID
		if err := tx.Exec("SELECT pg_advisory_xact_lock(hashtextextended(?,0))", key).Error; err != nil {
			return err
		}
		var previous struct{ AlarmID int64 }
		if err := tx.Raw("SELECT alarm_id FROM agent_interpretations WHERE event_key=?", key).Scan(&previous).Error; err != nil {
			return err
		}
		var device model.Device
		if err := tx.Where("device_code=? AND deleted_at IS NULL", code).First(&device).Error; err != nil {
			return err
		}
		if err := TelemetryDeviceLock(tx, device.ID); err != nil {
			return err
		}
		version := ManualAPIDemoVersion
		if analysis == "fixed" {
			version = ManualFixedDemoVersion
		}
		if previous.AlarmID != 0 {
			if err := tx.First(&alarm, previous.AlarmID).Error; err != nil {
				return err
			}
			if !IsManualDemo(alarm) || alarm.RuleVersion != version || alarm.DeviceID != device.ID || alarm.Level != level {
				return errors.New("demo_request_id_options_changed")
			}
			return nil
		}
		var active model.AlarmRecord
		if err := tx.Where("device_id=? AND metric=? AND recovered_at IS NULL", device.ID, SimulationMetric).First(&active).Error; err == nil {
			return fmt.Errorf("demo_already_active:%d", active.ID)
		} else if !errors.Is(err, gorm.ErrRecordNotFound) {
			return err
		}
		now := time.Now().UTC()
		alarm = model.AlarmRecord{DeviceID: device.ID, Metric: SimulationMetric, Level: level, Value: 1, Threshold: 0, Status: "unhandled", Origin: SimulationOrigin, RuleOperator: ">", RuleVersion: version, TriggeredAt: now}
		if err := tx.Create(&alarm).Error; err != nil {
			return err
		}
		if err := QueueAlarmNotification(tx, device, alarm, "triggered"); err != nil {
			return err
		}
		if err := tx.Exec(`INSERT INTO agent_interpretations(event_key,category,alarm_id,level,occurred_at,protocol_version) VALUES(?,'device_alarm',?,?,?,2)`, key, alarm.ID, level, now).Error; err != nil {
			return err
		}
		created = true
		return nil
	})
	return alarm, created, err
}

func EndManualAPIDemo(db *gorm.DB, id int64) (model.AlarmRecord, bool, error) {
	var alarm model.AlarmRecord
	changed := false
	if id <= 0 {
		return alarm, false, errors.New("invalid_demo_alarm_id")
	}
	err := TelemetryTransaction(db, func(tx *gorm.DB) error {
		if err := tx.First(&alarm, id).Error; err != nil {
			return err
		}
		if !IsManualDemo(alarm) {
			return errors.New("not_a_manual_api_demo")
		}
		if err := TelemetryDeviceLock(tx, alarm.DeviceID); err != nil {
			return err
		}
		if err := tx.Clauses(clause.Locking{Strength: "UPDATE"}).First(&alarm, id).Error; err != nil {
			return err
		}
		if alarm.RecoveredAt != nil {
			return nil
		}
		var device model.Device
		if err := tx.First(&device, alarm.DeviceID).Error; err != nil {
			return err
		}
		now := time.Now().UTC()
		reason := "manual_demo_ended"
		if err := tx.Model(&alarm).Updates(map[string]any{"status": "recovered", "recovered_at": now, "close_reason": reason, "updated_at": now}).Error; err != nil {
			return err
		}
		alarm.Status = "recovered"
		alarm.RecoveredAt = &now
		alarm.CloseReason = &reason
		if err := QueueAlarmNotification(tx, device, alarm, "recovered"); err != nil {
			return err
		}
		changed = true
		return nil
	})
	return alarm, changed, err
}

func AckManualAPIDemo(db *gorm.DB, id int64, username string) (model.AlarmRecord, bool, error) {
	var alarm model.AlarmRecord
	changed := false
	err := TelemetryTransaction(db, func(tx *gorm.DB) error {
		if err := tx.First(&alarm, id).Error; err != nil {
			return err
		}
		if !IsManualDemo(alarm) {
			return errors.New("not_a_manual_api_demo")
		}
		if err := TelemetryDeviceLock(tx, alarm.DeviceID); err != nil {
			return err
		}
		if err := tx.Clauses(clause.Locking{Strength: "UPDATE"}).First(&alarm, id).Error; err != nil {
			return err
		}
		var actor model.User
		if err := tx.Where("username=? AND role IN ?", username, []string{"admin", "operator"}).First(&actor).Error; err != nil {
			return errors.New("demo_actor_not_found_or_not_allowed")
		}
		if alarm.Status == "acked" {
			return nil
		}
		var err error
		alarm, err = AcknowledgeAlarm(tx, id, actor.ID, time.Now().UTC())
		changed = err == nil
		return err
	})
	return alarm, changed, err
}
