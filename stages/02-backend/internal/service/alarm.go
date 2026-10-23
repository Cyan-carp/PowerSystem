package service

import (
	"errors"
	"time"

	"gorm.io/gorm"
	"gorm.io/gorm/clause"

	"powersystem/backend/internal/energy"
	"powersystem/backend/internal/model"
	"powersystem/backend/internal/telemetry"
)

type AlarmEvent struct {
	Type string            `json:"type"`
	Data model.AlarmRecord `json:"data"`
}

const DailyFleetNaturalAlarmLimit = 5

// The fleet is synthetic, but these are ordinary rule evaluations against
// persisted telemetry. The slot guard bounds only the known fleet; it must
// never suppress alarms from physical devices.
var fleetNaturalSlots = map[string]struct {
	hour   int
	metric string
}{
	"PV-01": {9, "temperature"},
	"PV-02": {11, "voltage"},
	"PV-03": {13, "temperature"},
	"PV-04": {15, "current"},
	"ST-01": {17, "power"},
}

func fleetSlotWindow(stationCode, metric string, at time.Time) bool {
	slot, ok := fleetNaturalSlots[stationCode]
	if !ok || slot.metric != metric {
		return false
	}
	local := at.In(SimulationLocation())
	return local.Hour() == slot.hour && local.Minute() < 10
}

func selectedFleetDevice(tx *gorm.DB, stationCode string, at time.Time) (int64, error) {
	var devices []SimulationPolicyDevice
	if err := tx.Where("station_code = ? AND active = ?", stationCode, true).
		Order("device_code ASC").Find(&devices).Error; err != nil {
		return 0, err
	}
	if len(devices) == 0 {
		return 0, nil
	}
	return devices[SimulationDeviceIndex(at, len(devices))].DeviceID, nil
}

func permitFleetNaturalAlarm(tx *gorm.DB, device model.Device, metric string, at, sampledAt time.Time) (bool, error) {
	// A delayed or replayed MQTT sample must not consume today's slot.
	if sampledAt.Before(at.Add(-2*time.Minute)) || sampledAt.After(at.Add(30*time.Second)) ||
		!fleetSlotWindow(device.StationCode, metric, at) || !fleetSlotWindow(device.StationCode, metric, sampledAt) {
		return false, nil
	}
	var verified int64
	if err := tx.Model(&SimulationPolicyDevice{}).Where("active = ?", true).Count(&verified).Error; err != nil {
		return false, err
	}
	if verified != SimulationExpectedDevices {
		return false, nil
	}
	selected, err := selectedFleetDevice(tx, device.StationCode, at)
	if err != nil || selected != device.ID {
		return false, err
	}
	// Serialize the five synthetic slots across API workers and devices. A
	// plain COUNT then INSERT is racy when multiple samples arrive together.
	if err := tx.Exec("SELECT pg_advisory_xact_lock(20261022, 5)").Error; err != nil {
		return false, err
	}
	day := SimulationDay(at).UTC()
	end := day.Add(24 * time.Hour)
	var previous int64
	if err := tx.Table("alarm_records").Where("device_id = ? AND metric = ? AND triggered_at >= ? AND triggered_at < ?", device.ID, metric, day, end).Count(&previous).Error; err != nil {
		return false, err
	}
	if previous != 0 {
		return false, nil
	}
	var count int64
	if err := tx.Table("alarm_records AS a").
		Joins("JOIN simulation_alarm_policy_devices AS p ON p.device_id = a.device_id AND p.active = TRUE").
		Where("a.origin IN ? AND a.triggered_at >= ? AND a.triggered_at < ?", []string{"normal", SimulationOrigin}, day, end).
		Distinct("a.id").Count(&count).Error; err != nil {
		return false, err
	}
	return count < DailyFleetNaturalAlarmLimit, nil
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
		// The persisted fleet identity scopes the five-slot cap. Recovery still
		// runs for every active alarm, including legacy demonstration records.
		controlled, err := IsControlledSimulationDevice(tx, device.ID)
		if err != nil {
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
				if controlled {
					allowed, err := permitFleetNaturalAlarm(tx, device, rule.Metric, now, time.UnixMilli(sample.TS))
					if err != nil {
						return err
					}
					if !allowed {
						continue
					}
				} else if device.IsSimulated {
					// If a known synthetic station lost its policy row, fail closed
					// rather than unleash unbounded alarms from the fleet.
					if _, known := fleetNaturalSlots[device.StationCode]; known {
						continue
					}
				}
				ruleID := rule.ID
				record = model.AlarmRecord{DeviceID: device.ID, RuleID: &ruleID, Metric: rule.Metric, Level: rule.Level, Value: value, Threshold: rule.Threshold, Status: "unhandled", RuleOperator: rule.Operator, RuleVersion: rule.Version, TriggeredAt: now}
				if err := tx.Create(&record).Error; err != nil {
					return err
				}
				if err := QueueAlarmNotification(tx, device, record, "triggered"); err != nil {
					return err
				}
				events = append(events, AlarmEvent{Type: "alarm_created", Data: record})
			}
			if !active && lookup == nil {
				if err := tx.Model(&record).Updates(map[string]any{"recovered_at": now, "status": "recovered", "close_reason": "telemetry_recovered", "updated_at": now}).Error; err != nil {
					return err
				}
				record.RecoveredAt = &now
				record.Status = "recovered"
				reason := "telemetry_recovered"
				record.CloseReason = &reason
				if err := QueueAlarmNotification(tx, device, record, "recovered"); err != nil {
					return err
				}
				events = append(events, AlarmEvent{Type: "alarm_recovered", Data: record})
			}
		}
		if err := tx.Model(&model.Inbox{}).Where("id=? AND processed_at IS NULL", item.ID).Update("processed_at", now).Error; err != nil {
			return err
		}
		return energy.MarkDirty(tx, item.DeviceID, sample.TS)
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
