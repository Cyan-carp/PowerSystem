package service

import (
	"errors"
	"time"

	"gorm.io/gorm"
	"gorm.io/gorm/clause"

	"powersystem/backend/internal/model"
	"powersystem/backend/internal/telemetry"
)

type AlarmEvent struct {
	Type string            `json:"type"`
	Data model.AlarmRecord `json:"data"`
}

// ProcessSample changes alarms and marks the durable inbox item processed in
// one PostgreSQL transaction. Acknowledgement does not close an active alarm.
func ProcessSample(db *gorm.DB, item model.Inbox, sample telemetry.Sample) (model.Device, []AlarmEvent, error) {
	var device model.Device
	var events []AlarmEvent
	err := db.Transaction(func(tx *gorm.DB) error {
		if err := tx.First(&device, item.DeviceID).Error; err != nil {
			return err
		}
		var rules []model.AlarmRule
		if err := tx.Clauses(clause.Locking{Strength: "UPDATE"}).Where("device_id = ? AND enabled = true", device.ID).Find(&rules).Error; err != nil {
			return err
		}
		now := time.Now().UTC()
		for _, rule := range rules {
			value := sample.Value(rule.Metric)
			active := Matches(rule.Operator, value, rule.Threshold)
			var record model.AlarmRecord
			lookup := tx.Where("device_id=? AND metric=? AND recovered_at IS NULL", device.ID, rule.Metric).First(&record).Error
			if lookup != nil && !errors.Is(lookup, gorm.ErrRecordNotFound) {
				return lookup
			}
			if active && errors.Is(lookup, gorm.ErrRecordNotFound) {
				ruleID := rule.ID
				record = model.AlarmRecord{DeviceID: device.ID, RuleID: &ruleID, Metric: rule.Metric, Level: rule.Level, Value: value, Threshold: rule.Threshold, Status: "unhandled", TriggeredAt: now}
				if err := tx.Create(&record).Error; err != nil {
					return err
				}
				events = append(events, AlarmEvent{Type: "alarm_created", Data: record})
			}
			if !active && lookup == nil {
				if err := tx.Model(&record).Updates(map[string]any{"recovered_at": now, "status": "recovered", "updated_at": now}).Error; err != nil {
					return err
				}
				record.RecoveredAt = &now
				record.Status = "recovered"
				events = append(events, AlarmEvent{Type: "alarm_recovered", Data: record})
			}
		}
		return tx.Model(&model.Inbox{}).Where("id=? AND processed_at IS NULL", item.ID).Update("processed_at", now).Error
	})
	return device, events, err
}

func Matches(operator string, value, threshold float64) bool {
	switch operator {
	case ">":
		return value > threshold
	case ">=":
		return value >= threshold
	case "<":
		return value < threshold
	case "<=":
		return value <= threshold
	}
	return false
}
