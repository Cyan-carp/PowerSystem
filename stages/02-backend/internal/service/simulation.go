package service

import (
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"gorm.io/gorm"
	"gorm.io/gorm/clause"

	"powersystem/backend/internal/fleet"
	"powersystem/backend/internal/model"
)

// 受控演示告警：只写告警/通知事实，不写遥测和预测。
// 与自然告警通过 metric、origin 和 scenario_id 区分。
const (
	SimulationModeOff        = "off"
	SimulationModeControlled = "controlled"
	SimulationModePaused     = "paused"

	SimulationOrigin = "simulation"
	SimulationMetric = "simulation_scenario"

	// SimulationExpectedDevices 是当前 fleet.json 的完整设备数：140 台业务 + ST-01 三台。
	SimulationExpectedDevices = 143

	// SimulationExecutionWindow 是槽位到时后允许执行的窗口，超时只记 skipped。
	SimulationExecutionWindow = 60 * time.Second
	// SimulationDemoDuration 是单次演示告警的持续时长。
	SimulationDemoDuration = 10 * time.Minute
	// SimulationTelemetryFreshness 是最新遥测允许的最大时延。
	SimulationTelemetryFreshness = 2 * time.Minute
)

const (
	simulationReasonBeforeEnable = "before_first_enable"
	simulationReasonWindowMissed = "window_missed"
	simulationReasonStarted      = "simulation_demo_started"
	simulationReasonEnded        = "simulation_demo_ended"
)

// SimulationLocation 固定使用 Asia/Shanghai（UTC+8）。固定偏移不依赖容器 tzdata，
// 与现行中国时区一致，也不存在夏令时。
func SimulationLocation() *time.Location {
	return time.FixedZone("Asia/Shanghai", 8*60*60)
}

// SimulationSlotPlan 是某一天的单个日程槽位。
type SimulationSlotPlan struct {
	Day         time.Time
	Hour        int
	StationCode string
}

var simulationSlotStations = []struct {
	hour    int
	station string
}{
	{9, "PV-01"},
	{11, "PV-02"},
	{13, "PV-03"},
	{15, "PV-04"},
	{17, "ST-01"},
}

// SimulationDay 返回该时刻所在上海日历日的零点。
func SimulationDay(at time.Time) time.Time {
	local := at.In(SimulationLocation())
	return time.Date(local.Year(), local.Month(), local.Day(), 0, 0, 0, 0, SimulationLocation())
}

// SimulationSchedule 返回固定五个槽位，依次对应四个业务站和 ST-01。
func SimulationSchedule(day time.Time) []SimulationSlotPlan {
	midnight := SimulationDay(day)
	plans := make([]SimulationSlotPlan, 0, len(simulationSlotStations))
	for _, entry := range simulationSlotStations {
		plans = append(plans, SimulationSlotPlan{Day: midnight, Hour: entry.hour, StationCode: entry.station})
	}
	return plans
}

// SimulationScheduledAt 返回槽位的计划执行时刻。
func SimulationScheduledAt(day time.Time, hour int) time.Time {
	return SimulationDay(day).Add(time.Duration(hour) * time.Hour)
}

// SimulationDeviceIndex 按日期稳定选择同站设备。重启、重跑不会洗牌；
// 次日整体前移一台，设备数天后循环。
func SimulationDeviceIndex(day time.Time, count int) int {
	if count <= 0 {
		return 0
	}
	days := SimulationDay(day).Unix() / int64(24*time.Hour/time.Second)
	if days < 0 {
		days = -days
	}
	return int(days % int64(count))
}

// SimulationIdentity 是清单中的场站+设备编码。
type SimulationIdentity struct {
	StationCode string
	DeviceCode  string
}

// SimulationCatalogIdentities 按清单顺序展开全部设备编码。
// 业务站使用 <station>-<prefix>-NN，基线站使用 explicit 清单。
func SimulationCatalogIdentities(catalog *fleet.Catalog) ([]SimulationIdentity, error) {
	if catalog == nil {
		return nil, errors.New("fleet catalog unavailable")
	}
	identities := make([]SimulationIdentity, 0, SimulationExpectedDevices)
	seen := make(map[string]bool, SimulationExpectedDevices)
	for _, station := range catalog.Stations {
		codes := append([]string(nil), station.Explicit...)
		if station.Scope == fleet.ScopeBusiness {
			codes = make([]string, 0, station.ExpectedDevices)
			for index := 1; index <= station.ExpectedDevices; index++ {
				codes = append(codes, fmt.Sprintf("%s-%s-%02d", station.Code, station.DevicePrefix, index))
			}
		}
		for _, code := range codes {
			if seen[code] {
				return nil, fmt.Errorf("duplicate fleet device_code %q", code)
			}
			seen[code] = true
			identities = append(identities, SimulationIdentity{StationCode: station.Code, DeviceCode: code})
		}
	}
	return identities, nil
}

// SimulationPolicyReport 是一次清单核对的结果。OK 仅当清单恰为预期规模且全部台账匹配。
// 无论 OK 与否，已核对的设备都会持久化并继续抑制自然新建。
type SimulationPolicyReport struct {
	Expected   int
	Verified   int
	Missing    []string
	Mismatched []string
	OK         bool
}

// SimulationPolicyDevice 是持久受控设备身份。
type SimulationPolicyDevice struct {
	ID          int64     `gorm:"primaryKey"`
	StationCode string    `gorm:"column:station_code"`
	DeviceCode  string    `gorm:"column:device_code"`
	DeviceID    int64     `gorm:"column:device_id"`
	Source      string    `gorm:"column:source"`
	Active      bool      `gorm:"column:active"`
	VerifiedAt  time.Time `gorm:"column:verified_at"`
	CreatedAt   time.Time `gorm:"column:created_at"`
	UpdatedAt   time.Time `gorm:"column:updated_at"`
}

func (SimulationPolicyDevice) TableName() string { return "simulation_alarm_policy_devices" }

// SimulationSlot 是日程槽位及其取证元数据。
type SimulationSlot struct {
	ID          int64      `gorm:"primaryKey"`
	SlotDate    time.Time  `gorm:"column:slot_date"`
	SlotHour    int        `gorm:"column:slot_hour"`
	StationCode string     `gorm:"column:station_code"`
	DeviceID    int64      `gorm:"column:device_id"`
	DeviceCode  string     `gorm:"column:device_code"`
	Status      string     `gorm:"column:status"`
	Reason      *string    `gorm:"column:reason"`
	AlarmID     *int64     `gorm:"column:alarm_id"`
	ScheduledAt time.Time  `gorm:"column:scheduled_at"`
	StartedAt   *time.Time `gorm:"column:started_at"`
	EndedAt     *time.Time `gorm:"column:ended_at"`
	CreatedAt   time.Time  `gorm:"column:created_at"`
	UpdatedAt   time.Time  `gorm:"column:updated_at"`
}

func (SimulationSlot) TableName() string { return "simulation_alarm_slots" }

// EnsureSimulationPolicyDevices 按场站编码核对 devices 台账并持久化已核对身份。
// 缺失或场站不一致只记入报告，不写入身份；既有身份不删除、不停用。
func EnsureSimulationPolicyDevices(db *gorm.DB, catalog *fleet.Catalog) (SimulationPolicyReport, error) {
	identities, err := SimulationCatalogIdentities(catalog)
	if err != nil {
		return SimulationPolicyReport{}, err
	}
	report := SimulationPolicyReport{
		Expected:   len(identities),
		Missing:    []string{},
		Mismatched: []string{},
	}
	type verifiedDevice struct {
		identity SimulationIdentity
		deviceID int64
	}
	verified := make([]verifiedDevice, 0, len(identities))
	for _, identity := range identities {
		var row struct {
			ID          int64
			StationCode string `gorm:"column:station_code"`
		}
		err := db.Table("devices").
			Select("id, station_code").
			Where("device_code = ? AND deleted_at IS NULL", identity.DeviceCode).
			Take(&row).Error
		if errors.Is(err, gorm.ErrRecordNotFound) {
			report.Missing = append(report.Missing, identity.StationCode+"/"+identity.DeviceCode)
			continue
		}
		if err != nil {
			return report, err
		}
		if row.StationCode != identity.StationCode {
			report.Mismatched = append(report.Mismatched, identity.StationCode+"/"+identity.DeviceCode)
			continue
		}
		verified = append(verified, verifiedDevice{identity: identity, deviceID: row.ID})
	}
	report.Verified = len(verified)
	report.OK = report.Expected == SimulationExpectedDevices && report.Verified == report.Expected

	now := time.Now().UTC()
	err = db.Transaction(func(tx *gorm.DB) error {
		for _, device := range verified {
			row := SimulationPolicyDevice{
				StationCode: device.identity.StationCode,
				DeviceCode:  device.identity.DeviceCode,
				DeviceID:    device.deviceID,
				Source:      "fleet_catalog",
				Active:      true,
				VerifiedAt:  now,
				CreatedAt:   now,
				UpdatedAt:   now,
			}
			if err := tx.Clauses(clause.OnConflict{
				Columns: []clause.Column{{Name: "station_code"}, {Name: "device_code"}},
				DoUpdates: clause.Assignments(map[string]any{
					"device_id":   device.deviceID,
					"source":      "fleet_catalog",
					"active":      true,
					"verified_at": now,
					"updated_at":  now,
				}),
			}).Create(&row).Error; err != nil {
				return err
			}
		}
		return nil
	})
	return report, err
}

// simulationPolicyDevices 按设备编码排序返回某站已核对的受控设备。
func simulationPolicyDevices(db *gorm.DB, stationCode string) ([]SimulationPolicyDevice, error) {
	var devices []SimulationPolicyDevice
	err := db.Table("simulation_alarm_policy_devices").
		Where("station_code = ? AND active = ?", stationCode, true).
		Order("device_code ASC").
		Find(&devices).Error
	return devices, err
}

// IsControlledSimulationDevice 只读持久身份表，作为自然告警新建门控。
// 清单核对失败不影响既有身份，受控设备不会退回自然告警。
func IsControlledSimulationDevice(db *gorm.DB, deviceID int64) (bool, error) {
	var row struct {
		ID int64
	}
	err := db.Table("simulation_alarm_policy_devices").
		Select("id").
		Where("device_id = ? AND active = ?", deviceID, true).
		Take(&row).Error
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	return row.ID != 0, nil
}

// SimulationFirstEnabledAt 读取或写入首次启用时刻，只写一次，不覆盖。
func SimulationFirstEnabledAt(db *gorm.DB, now time.Time) (time.Time, error) {
	now = now.UTC()
	if err := db.Exec(`INSERT INTO simulation_alarm_state(id, first_enabled_at, updated_at)
		VALUES(1, ?, ?) ON CONFLICT (id) DO NOTHING`, now, now).Error; err != nil {
		return time.Time{}, err
	}
	if err := db.Exec(`UPDATE simulation_alarm_state SET first_enabled_at = ?, updated_at = ?
		WHERE id = 1 AND first_enabled_at IS NULL`, now, now).Error; err != nil {
		return time.Time{}, err
	}
	var row struct {
		FirstEnabledAt *time.Time `gorm:"column:first_enabled_at"`
	}
	if err := db.Table("simulation_alarm_state").Select("first_enabled_at").Where("id = 1").Take(&row).Error; err != nil {
		return time.Time{}, err
	}
	if row.FirstEnabledAt == nil {
		return now, nil
	}
	return row.FirstEnabledAt.UTC(), nil
}

// EnsureSimulationSlots 为当天创建五个槽位。已存在的槽位保持原状态（含 fired/ended），
// 首次启用前的槽位只记 skipped，不补发。
func EnsureSimulationSlots(db *gorm.DB, now, firstEnabledAt time.Time) error {
	now = now.UTC()
	for _, plan := range SimulationSchedule(now) {
		devices, err := simulationPolicyDevices(db, plan.StationCode)
		if err != nil {
			return err
		}
		if len(devices) == 0 {
			return fmt.Errorf("simulation policy has no devices for station %s", plan.StationCode)
		}
		device := devices[SimulationDeviceIndex(plan.Day, len(devices))]
		scheduled := SimulationScheduledAt(plan.Day, plan.Hour)
		status := "pending"
		var reason *string
		if !firstEnabledAt.IsZero() && scheduled.Before(firstEnabledAt) {
			status = "skipped"
			value := simulationReasonBeforeEnable
			reason = &value
		} else if now.After(scheduled.Add(SimulationExecutionWindow)) {
			status = "skipped"
			value := simulationReasonWindowMissed
			reason = &value
		}
		if err := db.Exec(`INSERT INTO simulation_alarm_slots(
			slot_date, slot_hour, station_code, device_id, device_code, status, reason,
			scheduled_at, created_at, updated_at)
			VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
			ON CONFLICT (slot_date, slot_hour) DO NOTHING`,
			plan.Day.Format("2006-01-02"), plan.Hour, plan.StationCode, device.DeviceID,
			device.DeviceCode, status, reason, scheduled, now, now).Error; err != nil {
			return err
		}
	}
	return nil
}

// PendingDueSimulationSlots 返回已到执行窗口、仍待触发的槽位。
func PendingDueSimulationSlots(db *gorm.DB, now time.Time) ([]SimulationSlot, error) {
	now = now.UTC()
	var slots []SimulationSlot
	err := db.Table("simulation_alarm_slots").
		Where("status = ? AND scheduled_at <= ? AND scheduled_at > ?", "pending", now, now.Add(-SimulationExecutionWindow)).
		Order("scheduled_at ASC, id ASC").
		Find(&slots).Error
	return slots, err
}

// SkipSimulationSlot 幂等记录跳过原因；已触发或已结束的槽位不受影响。
func SkipSimulationSlot(db *gorm.DB, slotID int64, reason string) error {
	return db.Table("simulation_alarm_slots").
		Where("id = ? AND status = ?", slotID, "pending").
		Updates(map[string]any{"status": "skipped", "reason": reason, "updated_at": time.Now().UTC()}).Error
}

// SkipMissedSimulationSlots 把超过执行窗口仍未触发的槽位统一记为跳过，不补发。
func SkipMissedSimulationSlots(db *gorm.DB, now time.Time) error {
	now = now.UTC()
	return db.Table("simulation_alarm_slots").
		Where("status = ? AND scheduled_at <= ?", "pending", now.Add(-SimulationExecutionWindow)).
		Updates(map[string]any{"status": "skipped", "reason": simulationReasonWindowMissed, "updated_at": now}).Error
}

// SimulationLatestUsable 校验 Redis 最新遥测是否可以支撑演示触发。
// 返回 ok 与跳过原因；只读取，不修改遥测。
func SimulationLatestUsable(raw []byte, now time.Time) (bool, string) {
	var latest struct {
		TS         int64     `json:"ts_ms"`
		ReceivedAt time.Time `json:"received_at"`
		Status     int       `json:"status"`
		FaultCode  int       `json:"fault_code"`
	}
	if err := json.Unmarshal(raw, &latest); err != nil {
		return false, "invalid_latest_telemetry"
	}
	if latest.TS <= 0 || latest.ReceivedAt.IsZero() {
		return false, "invalid_latest_telemetry"
	}
	now = now.UTC()
	if now.Sub(latest.ReceivedAt.UTC()) > SimulationTelemetryFreshness {
		return false, "stale_telemetry"
	}
	sampled := time.UnixMilli(latest.TS)
	if now.Sub(sampled) > SimulationTelemetryFreshness || sampled.After(now.Add(time.Minute)) {
		return false, "stale_telemetry"
	}
	if latest.Status == 2 {
		return false, "device_offline"
	}
	if latest.FaultCode != 0 {
		return false, "device_fault"
	}
	return true, ""
}

// FireSimulationSlot 在单个事务内创建演示告警、写通知 outbox 并标记槽位。
// 调用方必须先完成 Redis 可用性检查。
func FireSimulationSlot(db *gorm.DB, slotID int64, now time.Time) (*AlarmEvent, error) {
	now = now.UTC()
	var event *AlarmEvent
	err := db.Transaction(func(tx *gorm.DB) error {
		var slot SimulationSlot
		if err := tx.Table("simulation_alarm_slots").
			Clauses(clause.Locking{Strength: "UPDATE"}).
			Where("id = ?", slotID).Take(&slot).Error; err != nil {
			if errors.Is(err, gorm.ErrRecordNotFound) {
				return nil
			}
			return err
		}
		if slot.Status != "pending" {
			return nil
		}
		if now.Before(slot.ScheduledAt) {
			return nil
		}
		if now.After(slot.ScheduledAt.Add(SimulationExecutionWindow)) {
			return tx.Table("simulation_alarm_slots").
				Where("id = ? AND status = ?", slot.ID, "pending").
				Updates(map[string]any{"status": "skipped", "reason": simulationReasonWindowMissed, "updated_at": now}).Error
		}
		var device model.Device
		if err := tx.Where("id = ? AND deleted_at IS NULL", slot.DeviceID).First(&device).Error; err != nil {
			if errors.Is(err, gorm.ErrRecordNotFound) {
				return tx.Table("simulation_alarm_slots").
					Where("id = ? AND status = ?", slot.ID, "pending").
					Updates(map[string]any{"status": "skipped", "reason": "device_unavailable", "updated_at": now}).Error
			}
			return err
		}
		var alarmID int64
		if err := tx.Raw(`INSERT INTO alarm_records(
			device_id, metric, level, value, threshold, status, triggered_at,
			created_at, updated_at, origin, scenario_id)
			VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id`,
			slot.DeviceID, SimulationMetric, "minor", 1.0, 0.0, "unhandled", now,
			now, now, SimulationOrigin, slot.ID).Row().Scan(&alarmID); err != nil {
			return err
		}
		alarm := model.AlarmRecord{
			ID: alarmID, DeviceID: slot.DeviceID, Metric: SimulationMetric, Level: "minor",
			Origin: SimulationOrigin, ScenarioID: &slot.ID,
			Value: 1, Threshold: 0, Status: "unhandled", TriggeredAt: now, CreatedAt: now, UpdatedAt: now,
		}
		if err := QueueSimulationAlarmNotification(tx, device, alarm, slot, "triggered"); err != nil {
			return err
		}
		if err := tx.Table("simulation_alarm_slots").
			Where("id = ? AND status = ?", slot.ID, "pending").
			Updates(map[string]any{
				"status": "fired", "reason": simulationReasonStarted,
				"alarm_id": alarmID, "started_at": now, "updated_at": now,
			}).Error; err != nil {
			return err
		}
		event = &AlarmEvent{Type: "alarm_created", Data: alarm}
		return nil
	})
	return event, err
}

// CloseExpiredSimulationSlots 结束已满十分钟的演示告警。
// 结束原因是 simulation_demo_ended，明确不是遥测或预测恢复。
// closed 的 status 使用 recovered 以保持既有告警过滤协议，同时保留专用 close_reason。
func CloseExpiredSimulationSlots(db *gorm.DB, now time.Time) ([]AlarmEvent, error) {
	now = now.UTC()
	cutoff := now.Add(-SimulationDemoDuration)
	events := make([]AlarmEvent, 0)
	err := db.Transaction(func(tx *gorm.DB) error {
		var slots []SimulationSlot
		if err := tx.Table("simulation_alarm_slots").
			Clauses(clause.Locking{Strength: "UPDATE"}).
			Where("status = ? AND started_at IS NOT NULL AND started_at <= ?", "fired", cutoff).
			Order("id ASC").Find(&slots).Error; err != nil {
			return err
		}
		for _, slot := range slots {
			updated := false
			if slot.AlarmID != nil {
				result := tx.Model(&model.AlarmRecord{}).
					Where("id = ? AND recovered_at IS NULL", *slot.AlarmID).
					Updates(map[string]any{
						"recovered_at": now, "status": "recovered",
						"close_reason": simulationReasonEnded, "updated_at": now,
					})
				if result.Error != nil {
					return result.Error
				}
				updated = result.RowsAffected > 0
			}
			if err := tx.Table("simulation_alarm_slots").
				Where("id = ? AND status = ?", slot.ID, "fired").
				Updates(map[string]any{
					"status": "ended", "reason": simulationReasonEnded,
					"ended_at": now, "updated_at": now,
				}).Error; err != nil {
				return err
			}
			if !updated || slot.AlarmID == nil {
				continue
			}
			var alarm model.AlarmRecord
			if err := tx.First(&alarm, *slot.AlarmID).Error; err != nil {
				return err
			}
			var device model.Device
			if err := tx.First(&device, slot.DeviceID).Error; err != nil {
				return err
			}
			if err := QueueSimulationAlarmNotification(tx, device, alarm, slot, "recovered"); err != nil {
				return err
			}
			events = append(events, AlarmEvent{Type: "alarm_recovered", Data: alarm})
		}
		return nil
	})
	return events, err
}
