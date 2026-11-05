package service

import (
	"context"
	"time"

	"gorm.io/gorm"

	"powersystem/backend/internal/model"
)

// 预测真值口径的唯一定义处。
//
// 训练（stages/03-ai-prediction/prediction/train.py）、质量回填与历史修正在这里共用同一组常量，
// 避免再次出现「训练按 1 小时、标注按 2 小时」的分叉。
const (
	// PredictionHorizonMS 是真值窗口长度：只认 (window_end, window_end+1h] 内的故障起点。
	PredictionHorizonMS = model.PredictionHorizonMS
	// PredictionLabelRule 是 1 小时口径的规则标记。旧口径行保持 legacy，不得冒作新口径。
	PredictionLabelRule = model.PredictionLabelRule
	// PredictionQualityEpochMS 是新形态质量统计起点（2026-11-02T07:20:00Z）。
	PredictionQualityEpochMS = model.PredictionQualityEpochMS
)

// StationDeviceCache 缓存「某站某天被注入的设备编码」。
// 设备列表与模拟器保持一致：按 device_code 升序取当日生效的受控设备，
// 这样 SimulationDeviceIndex 选中的正是实际被注入的那一台。
type StationDeviceCache struct {
	db       *gorm.DB
	ctx      context.Context
	devices  map[string][]string
	selected map[string]string
}

func NewStationDeviceCache(db *gorm.DB, ctx context.Context) *StationDeviceCache {
	return &StationDeviceCache{
		db: db, ctx: ctx,
		devices: map[string][]string{}, selected: map[string]string{},
	}
}

// SelectedDevice 返回某站某日槽位选中的设备编码，空串表示该站没有可注入设备。
func (c *StationDeviceCache) SelectedDevice(station string, day time.Time) (string, error) {
	key := station + "|" + day.Format("2006-01-02")
	if code, ok := c.selected[key]; ok {
		return code, nil
	}
	codes, ok := c.devices[station]
	if !ok {
		codes = make([]string, 0, 32)
		if err := c.db.WithContext(c.ctx).Table("simulation_alarm_policy_devices").
			Where("station_code = ? AND active = ?", station, true).
			Order("device_code ASC").
			Pluck("device_code", &codes).Error; err != nil {
			return "", err
		}
		c.devices[station] = codes
	}
	code := ""
	if len(codes) > 0 {
		code = codes[SimulationDeviceIndex(day, len(codes))]
	}
	c.selected[key] = code
	return code, nil
}

// WindowFault 判定预测窗口 (windowEndMS, windowEndMS+1h] 内该设备是否被注入越线。
// 规则与训练标签一致：只在槽位起始时刻落点，左开右闭；不接受任何前兆容差。
func (c *StationDeviceCache) WindowFault(deviceCode, stationCode string, windowEndMS int64) (bool, *int64, error) {
	winEnd := windowEndMS + PredictionHorizonMS
	day := SimulationDay(time.UnixMilli(windowEndMS))
	last := SimulationDay(time.UnixMilli(winEnd))
	for !day.After(last) {
		for _, plan := range SimulationSchedule(day) {
			if plan.StationCode != stationCode {
				continue
			}
			start := SimulationScheduledAt(plan.Day, plan.Hour).UnixMilli()
			if start <= windowEndMS || start > winEnd {
				continue
			}
			code, err := c.SelectedDevice(plan.StationCode, plan.Day)
			if err != nil {
				return false, nil, err
			}
			if code == deviceCode {
				onset := start
				return true, &onset, nil
			}
		}
		day = day.AddDate(0, 0, 1)
	}
	return false, nil, nil
}
