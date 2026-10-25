package api

import (
	"database/sql"
	"math"
	"net/http"
	"sort"
	"strconv"
	"time"

	"github.com/gin-gonic/gin"

	"powersystem/backend/internal/model"
)

// energyReportRuleVersion 标识本接口的固定口径：只读 energy_daily 的日聚合。
const (
	energyReportRuleVersion = "energy-daily-v1"
	energyReportMaxDays     = 31
	energyReportDayLayout   = "2006-01-02"
)

// energyReportZone 是业务自然日边界：北京时间，无夏令时。
var energyReportZone = time.FixedZone("Asia/Shanghai", 8*60*60)

// energyReportDevice 是本次选中的设备。设备范围与 scope/station_code/device_id
// 的交集一致，不存在的组合返回空列表而不是伪造设备。
type energyReportDevice struct {
	DeviceID    int64  `json:"device_id"`
	DeviceCode  string `json:"device_code"`
	StationCode string `json:"station_code"`
}

// energyReportDay 是逐日聚合。KWh 为 null 表示当日没有有效聚合段，
// 不能当成零发电；DeviceCount 是选中设备数，ValidDeviceCount 是当日有效的设备数。
type energyReportDay struct {
	Date             string   `json:"date"`
	KWh              *float64 `json:"kwh"`
	DeviceCount      int      `json:"device_count"`
	ValidDeviceCount int      `json:"valid_device_count"`
	Complete         bool     `json:"complete"`
}

// energyReportMissing 逐条列出缺测的具体设备与自然日。
type energyReportMissing struct {
	DeviceID   int64  `json:"device_id"`
	DeviceCode string `json:"device_code"`
	Date       string `json:"date"`
	Reason     string `json:"reason"`
}

type energyReport struct {
	Scope        string                `json:"scope"`
	StationCode  string                `json:"station_code"`
	DeviceID     *int64                `json:"device_id,omitempty"`
	StartDate    string                `json:"start_date"`
	EndDate      string                `json:"end_date"`
	Days         int                   `json:"days"`
	Devices      []energyReportDevice  `json:"devices"`
	Daily        []energyReportDay     `json:"daily"`
	TotalKWh     *float64              `json:"total_kwh"`
	AggregatedAt *string               `json:"aggregated_at"`
	Complete     bool                  `json:"complete"`
	Missing      []energyReportMissing `json:"missing"`
	RuleVersion  string                `json:"rule_version"`
	ComputedAt   string                `json:"computed_at"`
	Timezone     string                `json:"timezone"`
	GroupBy      string                `json:"group_by"`
	Groups       []energyReportGroup   `json:"groups"`
}

type energyReportGroup struct {
	DeviceID    int64                 `json:"device_id,omitempty"`
	StationCode string                `json:"station_code"`
	Daily       []energyReportDay     `json:"daily"`
	TotalKWh    *float64              `json:"total_kwh"`
	Complete    bool                  `json:"complete"`
	Missing     []energyReportMissing `json:"missing"`
}

// energyReportRow 是 energy_daily 的只读投影。Day 使用 CAST 兼容 PostgreSQL DATE
// 与测试用 SQLite TEXT 两种存储。
type energyReportRow struct {
	DeviceID      int64           `gorm:"column:device_id"`
	Day           string          `gorm:"column:day"`
	KWh           sql.NullFloat64 `gorm:"column:kwh"`
	SegmentsCount sql.NullInt64   `gorm:"column:segments_count"`
	UpdatedAt     sql.NullTime    `gorm:"column:updated_at"`
}

// parseEnergyReportWindow 解析北京时间自然日窗口：end_date 不含，1..31 天，
// 不接受未来日期，也不接受被 Go 规范化后的非法日期（例如 2026-02-31）。
func parseEnergyReportWindow(c *gin.Context) (time.Time, time.Time, int, bool) {
	startRaw, endRaw := c.Query("start_date"), c.Query("end_date")
	if startRaw == "" || endRaw == "" {
		fail(c, http.StatusBadRequest, 40001, "start_date and end_date are required")
		return time.Time{}, time.Time{}, 0, false
	}
	start, err := time.ParseInLocation(energyReportDayLayout, startRaw, energyReportZone)
	if err != nil || start.Format(energyReportDayLayout) != startRaw {
		fail(c, http.StatusBadRequest, 40001, "invalid start_date")
		return time.Time{}, time.Time{}, 0, false
	}
	end, err := time.ParseInLocation(energyReportDayLayout, endRaw, energyReportZone)
	if err != nil || end.Format(energyReportDayLayout) != endRaw {
		fail(c, http.StatusBadRequest, 40001, "invalid end_date")
		return time.Time{}, time.Time{}, 0, false
	}
	if !end.After(start) {
		fail(c, http.StatusBadRequest, 40001, "end_date must be after start_date")
		return time.Time{}, time.Time{}, 0, false
	}
	days := int(end.Sub(start) / (24 * time.Hour))
	if days < 1 || days > energyReportMaxDays {
		fail(c, http.StatusBadRequest, 40001, "window must be 1 to 31 days")
		return time.Time{}, time.Time{}, 0, false
	}
	now := time.Now().In(energyReportZone)
	today := time.Date(now.Year(), now.Month(), now.Day(), 0, 0, 0, 0, energyReportZone)
	if end.After(today.AddDate(0, 0, 1)) {
		fail(c, http.StatusBadRequest, 40001, "end_date must not be in the future")
		return time.Time{}, time.Time{}, 0, false
	}
	return start, end, days, true
}

// energyReportDevices 在既有 fleet scope 上再做 device_id 交集；组合不匹配时
// 返回空集，由响应明确 complete=false，而不是猜测设备归属。
func (s *Server) energyReportDevices(selection scopeSelection, deviceID int64) ([]model.Device, error) {
	query := selection.applyToDevices(s.db.Where("deleted_at IS NULL"))
	var devices []model.Device
	if err := query.Order("id ASC").Find(&devices).Error; err != nil {
		return nil, err
	}
	if deviceID < 1 {
		return devices, nil
	}
	selected := make([]model.Device, 0, 1)
	for _, device := range devices {
		if device.ID == deviceID {
			selected = append(selected, device)
		}
	}
	return selected, nil
}

func (s *Server) energyReportRows(devices []model.Device, startDay, lastDay string) ([]energyReportRow, error) {
	if len(devices) == 0 {
		return nil, nil
	}
	ids := make([]int64, len(devices))
	for i, device := range devices {
		ids[i] = device.ID
	}
	var rows []energyReportRow
	err := s.db.Raw(`SELECT device_id, CAST(day AS TEXT) AS day, kwh, segments_count, updated_at
FROM energy_daily WHERE device_id IN ? AND day >= ? AND day <= ?`, ids, startDay, lastDay).Scan(&rows).Error
	if err != nil {
		return nil, err
	}
	return rows, nil
}

type energyReportKey struct {
	deviceID int64
	day      string
}

// buildEnergyReport 只使用 energy_daily 的日聚合：segments_count>0 才是有效段，
// 缺行或无效段都不算零，逐条记入 missing。end 为不含的结束边界。
func buildEnergyReport(selection scopeSelection, deviceID int64, devices []model.Device, rows []energyReportRow, days []string, end time.Time, computedAt time.Time) energyReport {
	report := energyReport{
		Scope:       selection.Scope,
		StationCode: selection.StationCode,
		StartDate:   days[0],
		EndDate:     end.Format(energyReportDayLayout),
		Days:        len(days),
		Devices:     make([]energyReportDevice, 0, len(devices)),
		Daily:       make([]energyReportDay, len(days)),
		Missing:     make([]energyReportMissing, 0),
		RuleVersion: energyReportRuleVersion,
		ComputedAt:  computedAt.UTC().Format(time.RFC3339Nano),
		Timezone:    "Asia/Shanghai",
	}
	if deviceID > 0 {
		value := deviceID
		report.DeviceID = &value
	}
	for _, device := range devices {
		report.Devices = append(report.Devices, energyReportDevice{DeviceID: device.ID, DeviceCode: device.DeviceCode, StationCode: device.StationCode})
	}
	dayIndex := make(map[string]int, len(days))
	for i, day := range days {
		dayIndex[day] = i
		report.Daily[i] = energyReportDay{Date: day, DeviceCount: len(devices)}
	}
	index := make(map[energyReportKey]energyReportRow, len(rows))
	var aggregatedAt *time.Time
	for _, row := range rows {
		index[energyReportKey{deviceID: row.DeviceID, day: row.Day}] = row
		if row.UpdatedAt.Valid && (aggregatedAt == nil || row.UpdatedAt.Time.After(*aggregatedAt)) {
			value := row.UpdatedAt.Time
			aggregatedAt = &value
		}
	}
	sums := make([]float64, len(days))
	total := 0.0
	validRows := 0
	for _, device := range devices {
		for _, day := range days {
			i := dayIndex[day]
			row, found := index[energyReportKey{deviceID: device.ID, day: day}]
			switch {
			case !found:
				report.Missing = append(report.Missing, energyReportMissing{DeviceID: device.ID, DeviceCode: device.DeviceCode, Date: day, Reason: "no_record"})
				continue
			case !row.SegmentsCount.Valid || row.SegmentsCount.Int64 <= 0:
				report.Missing = append(report.Missing, energyReportMissing{DeviceID: device.ID, DeviceCode: device.DeviceCode, Date: day, Reason: "no_segments"})
				continue
			case !row.KWh.Valid:
				report.Missing = append(report.Missing, energyReportMissing{DeviceID: device.ID, DeviceCode: device.DeviceCode, Date: day, Reason: "no_value"})
				continue
			case math.IsNaN(row.KWh.Float64) || math.IsInf(row.KWh.Float64, 0) || row.KWh.Float64 < 0 || math.IsInf(total+row.KWh.Float64, 0):
				report.Missing = append(report.Missing, energyReportMissing{DeviceID: device.ID, DeviceCode: device.DeviceCode, Date: day, Reason: "invalid_value"})
				continue
			}
			sums[i] += row.KWh.Float64
			report.Daily[i].ValidDeviceCount++
			total += row.KWh.Float64
			validRows++
		}
	}
	for i := range report.Daily {
		if report.Daily[i].ValidDeviceCount > 0 {
			value := sums[i]
			report.Daily[i].KWh = &value
		}
		report.Daily[i].Complete = len(devices) > 0 && report.Daily[i].ValidDeviceCount == len(devices)
	}
	if validRows > 0 {
		value := total
		report.TotalKWh = &value
	}
	if aggregatedAt != nil {
		value := aggregatedAt.UTC().Format(time.RFC3339Nano)
		report.AggregatedAt = &value
	}
	report.Complete = len(devices) > 0 && len(report.Missing) == 0
	return report
}

// energyReport 是 S05 冻结契约中 GET /api/v1/reports/energy 的最小只读适配。
// 只读 energy_daily；不读瞬时功率或 24h 遥测，不生成导出文件。
func (s *Server) energyReport(c *gin.Context) {
	start, end, dayCount, valid := parseEnergyReportWindow(c)
	if !valid {
		return
	}
	deviceID := int64(0)
	if raw := c.Query("device_id"); raw != "" {
		parsed, err := strconv.ParseInt(raw, 10, 64)
		if err != nil || parsed < 1 {
			fail(c, http.StatusBadRequest, 40001, "invalid device_id")
			return
		}
		deviceID = parsed
	}
	selection, scopeErr := resolveScopeFilter(s.fleetCatalog, c.Query("scope"), "")
	if scopeErr != nil {
		fail(c, scopeErr.status, scopeErr.code, scopeErr.message)
		return
	}
	station := c.Query("station_code")
	stationFilter, stationErr := resolveScopeFilter(s.fleetCatalog, "all", station)
	if stationErr != nil {
		fail(c, stationErr.status, stationErr.code, stationErr.message)
		return
	}
	selection.StationCode = station
	noMatch := false
	if station != "" {
		if selection.Scope != "all" {
			stationScope, exists := s.fleetCatalog.ScopeOf(station)
			noMatch = !exists || stationScope != selection.Scope
		}
		selection.StationCodes = stationFilter.StationCodes
	}
	var devices []model.Device
	var err error
	if !noMatch {
		devices, err = s.energyReportDevices(selection, deviceID)
	}
	if err != nil {
		fail(c, http.StatusInternalServerError, 50000, "energy report query failed")
		return
	}
	days := make([]string, 0, dayCount)
	for day := start; day.Before(end); day = day.AddDate(0, 0, 1) {
		days = append(days, day.Format(energyReportDayLayout))
	}
	rows, err := s.energyReportRows(devices, days[0], days[len(days)-1])
	if err != nil {
		fail(c, http.StatusInternalServerError, 50000, "energy report query failed")
		return
	}
	computedAt := time.Now()
	report := buildEnergyReport(selection, deviceID, devices, rows, days, end, computedAt)
	report.GroupBy = c.DefaultQuery("group_by", "station")
	if report.GroupBy != "station" && report.GroupBy != "device" {
		fail(c, 400, 40001, "group_by must be station or device")
		return
	}
	groupDevices := map[string][]model.Device{}
	for _, device := range devices {
		key := device.StationCode
		if report.GroupBy == "device" {
			key = strconv.FormatInt(device.ID, 10)
		}
		groupDevices[key] = append(groupDevices[key], device)
	}
	keys := make([]string, 0, len(groupDevices))
	for key := range groupDevices {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	report.Groups = make([]energyReportGroup, 0, len(keys))
	for _, key := range keys {
		selected := groupDevices[key]
		ids := map[int64]bool{}
		for _, d := range selected {
			ids[d.ID] = true
		}
		selectedRows := []energyReportRow{}
		for _, row := range rows {
			if ids[row.DeviceID] {
				selectedRows = append(selectedRows, row)
			}
		}
		group := buildEnergyReport(selection, 0, selected, selectedRows, days, end, computedAt)
		entry := energyReportGroup{StationCode: selected[0].StationCode, Daily: group.Daily, TotalKWh: group.TotalKWh, Complete: group.Complete, Missing: group.Missing}
		if report.GroupBy == "device" {
			entry.DeviceID = selected[0].ID
		}
		report.Groups = append(report.Groups, entry)
	}
	if c.Query("format") == "csv" {
		header := []string{"记录类型", "分组", "场站编码", "设备ID", "日期", "发电量(kWh)", "完整", "缺口原因", "开始日期(含)", "结束日期(不含)", "时区", "范围", "聚合更新时间", "计算时间", "来源", "规则版本"}
		csvRows := [][]string{}
		aggregated := ""
		if report.AggregatedAt != nil {
			aggregated = *report.AggregatedAt
		}
		appendRow := func(kind, station, device, date string, value *float64, complete bool, reason string) {
			csvRows = append(csvRows, []string{kind, report.GroupBy, station, device, date, reportNumber(value), strconv.FormatBool(complete), reason, report.StartDate, report.EndDate, report.Timezone, report.Scope, aggregated, report.ComputedAt, "energy_daily", report.RuleVersion})
		}
		appendRow("total", report.StationCode, c.Query("device_id"), "", report.TotalKWh, report.Complete, "")
		for _, group := range report.Groups {
			gid := ""
			if group.DeviceID > 0 {
				gid = strconv.FormatInt(group.DeviceID, 10)
			}
			appendRow("group_total", group.StationCode, gid, "", group.TotalKWh, group.Complete, "")
			for _, day := range group.Daily {
				appendRow("daily", group.StationCode, gid, day.Date, day.KWh, day.Complete, "")
			}
		}
		for _, gap := range report.Missing {
			appendRow("missing", "", strconv.FormatInt(gap.DeviceID, 10), gap.Date, nil, false, gap.Reason)
		}
		writeReportCSV(c, "energy.csv", report.RuleVersion, report.ComputedAt, report.Complete, header, csvRows)
		return
	}
	ok(c, report)
}
