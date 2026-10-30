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
	"PV-04": {15, "temperature"},
	"ST-01": {17, "temperature"},
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
		Where("NOT (a.metric=? AND COALESCE(a.rule_version,'') IN (?,?))", SimulationMetric, ManualAPIDemoVersion, ManualFixedDemoVersion).
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
		if err := AlarmWriteGate(tx); err != nil {
			return err
		}
		if err := TelemetryDeviceLock(tx, item.DeviceID); err != nil {
			return err
		}
		var current model.Inbox
		if err := tx.Clauses(clause.Locking{Strength: "UPDATE"}).First(&current, item.ID).Error; err != nil {
			return err
		}
		if current.ProcessedAt != nil {
			return ErrInboxTerminal
		}
		if err := tx.First(&device, item.DeviceID).Error; err != nil {
			return err
		}
		var err error
		events, err = ProcessLockedSample(tx, current, sample, device)
		return err
	})
	return device, events, err
}

// ProcessLockedSample shares the caller's already gated transaction, device
// advisory lock and inbox row lock. It never commits or publishes independently.
// ProcessSample remains the transaction-owning compatibility entry point.
func ProcessLockedSample(tx *gorm.DB, item model.Inbox, sample telemetry.Sample, device model.Device) ([]AlarmEvent, error) {
	return processLockedSample(tx, item, sample, device, true, time.Now().UTC())
}

// EvaluateRealtimeAlarms evaluates a verified fresh TD source without finishing
// its ordered historical business receipt. The caller owns maintenance/device/
// inbox locks. Both paths share the durable alarm watermark and outbox.
func EvaluateRealtimeAlarms(tx *gorm.DB, item model.Inbox, sample telemetry.Sample, device model.Device) ([]AlarmEvent, error) {
	if item.TDConfirmedAt == nil {
		return nil, errors.New("alarm_source_not_td_confirmed")
	}
	if item.ProcessedAt != nil || item.AlarmEvaluation != nil || telemetry.Freshness(time.Now().UTC(), item.ReceivedAt, sample.TS) != "fresh" {
		return nil, nil
	}
	return processLockedSample(tx, item, sample, device, false, time.Now().UTC())
}

func processLockedSample(tx *gorm.DB, item model.Inbox, sample telemetry.Sample, device model.Device, completeBusiness bool, now time.Time) ([]AlarmEvent, error) {
	var events []AlarmEvent
	err := func() error {
		evaluation := "evaluated"
		alreadyEvaluated := item.AlarmEvaluation != nil && *item.AlarmEvaluation == "evaluated"
		shouldEvaluate := !alreadyEvaluated
		var rules []model.AlarmRule
		controlled := false
		if !alreadyEvaluated && telemetry.Freshness(now, item.ReceivedAt, sample.TS) != "fresh" {
			evaluation = "historical_suppressed"
			shouldEvaluate = false
		}
		if tx.Dialector.Name() == "postgres" && shouldEvaluate {
			var accepted struct{ InboxID int64 }
			result := tx.Raw(`INSERT INTO telemetry_alarm_progress(device_id,inbox_id,ts_ms) VALUES(?,?,?) ON CONFLICT(device_id) DO UPDATE SET inbox_id=excluded.inbox_id,ts_ms=excluded.ts_ms WHERE telemetry_alarm_progress.inbox_id<excluded.inbox_id AND telemetry_alarm_progress.ts_ms<=excluded.ts_ms RETURNING inbox_id`, item.DeviceID, item.ID, sample.TS).Scan(&accepted)
			if result.Error != nil {
				return result.Error
			}
			if accepted.InboxID == 0 {
				evaluation = "historical_suppressed"
				shouldEvaluate = false
			}
		}
		if shouldEvaluate {
			var err error
			controlled, err = IsControlledSimulationDevice(tx, device.ID)
			if err != nil {
				return err
			}
			if err := tx.Clauses(clause.Locking{Strength: "UPDATE"}).Where("device_id=? AND enabled=true", device.ID).Find(&rules).Error; err != nil {
				return err
			}
		}
		// Four supported metrics share one bounded indexed lookup, retaining the
		// same device/rule/record lock order and per-rule trigger/recovery logic.
		activeByMetric := make(map[string]model.AlarmRecord, len(rules))
		if len(rules) > 0 {
			metrics := make([]string, 0, len(rules))
			for _, rule := range rules {
				metrics = append(metrics, rule.Metric)
			}
			var records []model.AlarmRecord
			if e := tx.Clauses(clause.Locking{Strength: "UPDATE"}).Where("device_id=? AND metric IN ? AND recovered_at IS NULL", device.ID, metrics).Order("id").Find(&records).Error; e != nil {
				return e
			}
			for _, record := range records {
				if _, exists := activeByMetric[record.Metric]; exists {
					return errors.New("alarm_active_duplicate")
				}
				activeByMetric[record.Metric] = record
			}
		}
		for _, rule := range rules {
			value := sample.Value(rule.Metric)
			active := Matches(rule.Operator, value, rule.Threshold)
			record, exists := activeByMetric[rule.Metric]
			if active && !exists {
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
			if !active && exists {
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
		if !completeBusiness {
			return tx.Model(&model.Inbox{}).Where("id=? AND processed_at IS NULL AND alarm_evaluation IS NULL", item.ID).Update("alarm_evaluation", evaluation).Error
		}
		if err := tx.Model(&model.Inbox{}).Where("id=? AND processed_at IS NULL", item.ID).Updates(map[string]any{"processed_at": now, "business_state": "success", "failure_code": nil, "next_attempt_at": nil, "alarm_evaluation": evaluation}).Error; err != nil {
			return err
		}
		return energy.MarkDirty(tx, item.DeviceID, sample.TS)
	}()
	return events, err
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
