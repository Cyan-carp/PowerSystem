package api

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"math"
	"net/http"
	"strconv"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/redis/go-redis/v9"
	"go.uber.org/zap"

	"powersystem/backend/internal/model"
	"powersystem/backend/internal/telemetry"
)

type overviewSummary struct {
	DeviceTotal              int      `json:"device_total"`
	Online                   int      `json:"online"`
	Offline                  int      `json:"offline"`
	TelemetryUnknown         int      `json:"telemetry_unknown"`
	Fault                    int      `json:"fault"`
	ActiveAlarms             int64    `json:"active_alarms"`
	CurrentPowerKW           *float64 `json:"current_power_kw"`
	OperationalHealthPercent *float64 `json:"operational_health_percent"`
	HealthScorePercent       *float64 `json:"health_score_percent"`
	HealthValidCount         int      `json:"health_valid_count"`
	HealthUnknownCount       int      `json:"health_unknown_count"`
	HealthProvisional        bool     `json:"health_provisional"`
	TodayEnergyKWh           *float64 `json:"today_energy_kwh"`
	EnergyProvisional        bool     `json:"energy_provisional"`
	EnergyUpdatedAt          *string  `json:"energy_updated_at"`
}

type overviewStation struct {
	StationCode           string `json:"station_code"`
	StationName           string `json:"station_name"`
	Scope                 string `json:"scope"`
	ExpectedDeviceCount   int    `json:"expected_device_count"`
	RegisteredDeviceCount int    `json:"registered_device_count"`
	ObservedDeviceCount   int    `json:"observed_device_count"`
	Partial               bool   `json:"partial"`
	overviewSummary
}

type overviewSnapshot struct {
	ReceivedAt *time.Time `json:"received_at"`
	TS         *int64     `json:"ts_ms"`
	Power      *float64   `json:"power"`
	Status     *int       `json:"status"`
}

type overviewAlarm struct {
	Count int64
	Level string
}

type overviewEnergy struct {
	Provisional bool
	KWh         *float64
	UpdatedAt   *time.Time
}

type overviewAccumulator struct {
	registered        int
	observed          int
	online            int
	offline           int
	fault             int
	power             float64
	powerCount        int
	alarms            int64
	healthSum         float64
	healthN           int
	energy            float64
	energyN           int
	energyProvisional bool
	energyAt          *time.Time
}

func (a overviewAccumulator) summary() overviewSummary {
	result := overviewSummary{
		DeviceTotal:        a.registered,
		Online:             a.online,
		Offline:            a.offline,
		TelemetryUnknown:   a.registered - a.online - a.offline,
		Fault:              a.fault,
		ActiveAlarms:       a.alarms,
		HealthValidCount:   a.healthN,
		HealthUnknownCount: a.registered - a.healthN,
		HealthProvisional:  a.healthN < a.registered,
	}
	if a.powerCount > 0 {
		value := math.Round(a.power*100) / 100
		result.CurrentPowerKW = &value
	}
	if a.registered > 0 {
		value := math.Round(100*float64(a.online-a.fault)/float64(a.registered)*100) / 100
		result.OperationalHealthPercent = &value
	}
	if a.healthN > 0 {
		value := math.Round(a.healthSum/float64(a.healthN)*100) / 100
		result.HealthScorePercent = &value
	}
	if a.energyN > 0 {
		value := math.Round(a.energy*100) / 100
		result.TodayEnergyKWh = &value
	}
	if a.energyAt != nil {
		value := a.energyAt.UTC().Format(time.RFC3339Nano)
		result.EnergyUpdatedAt = &value
	}
	result.EnergyProvisional = a.energyProvisional || a.energyN < a.registered
	return result
}

func snapshotIsCurrent(v overviewSnapshot, now time.Time) bool {
	if v.ReceivedAt == nil || v.TS == nil || v.Power == nil || v.Status == nil ||
		*v.Status < 0 || *v.Status > 2 || math.IsNaN(*v.Power) || math.IsInf(*v.Power, 0) {
		return false
	}
	return telemetry.Freshness(now, *v.ReceivedAt, *v.TS) == "fresh"
}

func healthValue(raw []byte, d model.Device, activeLevel string, now time.Time) *float64 {
	if len(raw) == 0 {
		return nil
	}
	h := scoreLatestHealth(deviceHealth{DeviceID: d.ID, DeviceCode: d.DeviceCode, Name: d.Name,
		StationCode: d.StationCode, State: "unknown", RuleVersion: healthRuleVersion}, raw, activeLevel, now)
	return h.Score
}

func (s *Server) latestSnapshots(ctx context.Context, devices []model.Device) map[int64][]byte {
	out := make(map[int64][]byte, len(devices))
	if len(devices) == 0 || s.redis == nil {
		return out
	}
	keys := make([]string, len(devices))
	for i, d := range devices {
		keys[i] = "device:" + strconv.FormatInt(d.ID, 10) + ":latest"
	}
	values, err := s.redis.MGet(ctx, keys...).Result()
	if err != nil {
		if s.log != nil && !errors.Is(err, redis.Nil) {
			s.log.Warn("overview_latest_unavailable", zap.Error(err))
		}
		return out
	}
	for i, value := range values {
		if text, ok := value.(string); ok {
			out[devices[i].ID] = []byte(text)
		}
	}
	return out
}

func (s *Server) overviewAlarms(devices []model.Device) (map[int64]overviewAlarm, error) {
	result := make(map[int64]overviewAlarm)
	if len(devices) == 0 {
		return result, nil
	}
	ids := make([]int64, len(devices))
	for i, d := range devices {
		ids[i] = d.ID
	}
	var rows []struct {
		DeviceID int64
		Total    int64
		Rank     int
	}
	if err := s.db.Model(&model.AlarmRecord{}).
		Select("device_id, count(*) AS total, max(CASE level WHEN 'urgent' THEN 3 WHEN 'major' THEN 2 WHEN 'minor' THEN 1 ELSE 0 END) AS rank").
		Where("recovered_at IS NULL AND device_id IN ?", ids).Group("device_id").Scan(&rows).Error; err != nil {
		return nil, err
	}
	for _, row := range rows {
		result[row.DeviceID] = overviewAlarm{Count: row.Total, Level: map[int]string{1: "minor", 2: "major", 3: "urgent"}[row.Rank]}
	}
	return result, nil
}

func (s *Server) overviewEnergy(devices []model.Device, now time.Time) (map[int64]overviewEnergy, error) {
	result := make(map[int64]overviewEnergy)
	if len(devices) == 0 {
		return result, nil
	}
	ids := make([]int64, len(devices))
	for i, d := range devices {
		ids[i] = d.ID
	}
	china := time.FixedZone("Asia/Shanghai", 8*60*60)
	day := now.In(china).Format("2006-01-02")
	var rows []struct {
		DeviceID  int64
		KWh       sql.NullFloat64 `gorm:"column:kwh"`
		Uncertain int64
		Updated   sql.NullTime
	}
	if err := s.db.Raw(`SELECT device_id, SUM(CASE WHEN segments_count>0 THEN kwh END) AS kwh,
SUM(CASE WHEN source_quality<>'verified' OR EXISTS(SELECT 1 FROM energy_dirty_days pending WHERE pending.device_id=energy_daily.device_id AND pending.day=energy_daily.day) THEN 1 ELSE 0 END) AS uncertain, MAX(updated_at) AS updated FROM energy_daily WHERE day=? AND device_id IN ? GROUP BY device_id`, day, ids).Scan(&rows).Error; err != nil {
		return nil, err
	}
	for _, row := range rows {
		item := overviewEnergy{Provisional: row.Uncertain > 0}
		if row.KWh.Valid {
			item.KWh = &row.KWh.Float64
		}
		if row.Updated.Valid {
			item.UpdatedAt = &row.Updated.Time
		}
		result[row.DeviceID] = item
	}
	return result, nil
}

func accumulateOverview(devices []model.Device, raw map[int64][]byte, alarms map[int64]overviewAlarm, energies map[int64]overviewEnergy, now time.Time) overviewAccumulator {
	result := overviewAccumulator{registered: len(devices)}
	for _, d := range devices {
		alarm := alarms[d.ID]
		result.alarms += alarm.Count
		bytes := raw[d.ID]
		var sample overviewSnapshot
		if len(bytes) > 0 && json.Unmarshal(bytes, &sample) == nil && snapshotIsCurrent(sample, now) {
			result.observed++
			result.online++
			result.power += *sample.Power
			result.powerCount++
			if *sample.Status == 2 {
				result.fault++
			}
		}
		if score := healthValue(bytes, d, alarm.Level, now); score != nil {
			result.healthSum += *score
			result.healthN++
		}
		if h := scoreLatestHealth(deviceHealth{State: "unknown"}, bytes, alarm.Level, now); h.State == "offline" {
			result.offline++
		}
		if energy := energies[d.ID]; energy.KWh != nil {
			result.energyProvisional = result.energyProvisional || energy.Provisional
			result.energy += *energy.KWh
			result.energyN++
			if energy.UpdatedAt != nil && (result.energyAt == nil || energy.UpdatedAt.After(*result.energyAt)) {
				value := *energy.UpdatedAt
				result.energyAt = &value
			}
		}
	}
	return result
}

func (s *Server) dashboardOverview(c *gin.Context) {
	if s.fleetCatalog == nil {
		fail(c, http.StatusServiceUnavailable, fleetUnavailableCode, "fleet configuration unavailable")
		return
	}
	selection, valid := s.scopeSelection(c)
	if !valid {
		return
	}
	var devices []model.Device
	query := selection.applyToDevices(s.db.Where("deleted_at IS NULL"))
	if err := query.Order("id ASC").Find(&devices).Error; err != nil {
		fail(c, http.StatusInternalServerError, 50000, "overview query failed")
		return
	}
	now := time.Now()
	raw := s.latestSnapshots(c.Request.Context(), devices)
	alarms, err := s.overviewAlarms(devices)
	if err != nil {
		fail(c, http.StatusInternalServerError, 50000, "overview alarm query failed")
		return
	}
	energies, err := s.overviewEnergy(devices, now)
	if err != nil {
		fail(c, http.StatusInternalServerError, 50000, "overview energy query failed")
		return
	}

	byStation := make(map[string][]model.Device)
	for _, d := range devices {
		byStation[d.StationCode] = append(byStation[d.StationCode], d)
	}
	stations := make([]overviewStation, 0)
	expected := 0
	for _, station := range s.fleetCatalog.StationsForScope(selection.Scope) {
		if selection.StationCode != "" && station.Code != selection.StationCode {
			continue
		}
		acc := accumulateOverview(byStation[station.Code], raw, alarms, energies, now)
		expected += station.ExpectedDevices
		stations = append(stations, overviewStation{
			StationCode: station.Code, StationName: station.Name, Scope: station.Scope,
			ExpectedDeviceCount: station.ExpectedDevices, RegisteredDeviceCount: acc.registered,
			ObservedDeviceCount: acc.observed, Partial: acc.registered < station.ExpectedDevices || acc.observed < acc.registered,
			overviewSummary: acc.summary(),
		})
	}
	total := accumulateOverview(devices, raw, alarms, energies, now)
	partial := total.registered < expected || total.observed < total.registered
	computedAt := now.UTC().Format(time.RFC3339Nano)
	ok(c, gin.H{
		"scope": selection.Scope, "station_code": selection.StationCode, "computed_at": computedAt,
		"partial": partial, "expected_device_count": expected, "registered_device_count": total.registered,
		"observed_device_count": total.observed, "health_rule_version": healthRuleVersion,
		"summary": total.summary(), "stations": stations,
	})
}
