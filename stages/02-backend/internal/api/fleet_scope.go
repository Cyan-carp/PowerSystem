package api

import (
	"net/http"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm"

	"powersystem/backend/internal/fleet"
	"powersystem/backend/internal/model"
	"powersystem/backend/internal/telemetry"
)

const fleetUnavailableCode = 50301

// scopeSelection 是解析后的范围条件。StationCodes 为空表示不限制场站。
type scopeSelection struct {
	Scope        string
	StationCode  string
	StationCodes []string
}

// scopeError 携带需要直接返回给调用方的 HTTP 状态。
type scopeError struct {
	status  int
	code    int
	message string
}

func (e *scopeError) Error() string { return e.message }

func invalidScopeError(message string) *scopeError {
	return &scopeError{status: http.StatusBadRequest, code: 40001, message: message}
}

func fleetUnavailableError() *scopeError {
	return &scopeError{status: http.StatusServiceUnavailable, code: fleetUnavailableCode, message: "fleet configuration unavailable"}
}

// resolveScopeFilter 实现统一范围规则：
//   - scope 仅允许 all|business|baseline，省略等于 all；
//   - station_code 必须符合遥测编码；
//   - 同时给出 scope 与 station_code 时，除 all 外必须与清单中的范围一致，否则 400；
//   - 需要清单（business/baseline 过滤）而清单不可用时返回 503。
func resolveScopeFilter(catalog *fleet.Catalog, scopeRaw, stationRaw string) (scopeSelection, *scopeError) {
	scope, err := fleet.NormalizeScope(scopeRaw)
	if err != nil {
		return scopeSelection{}, invalidScopeError("invalid scope")
	}
	if stationRaw != "" && !telemetry.Code.MatchString(stationRaw) {
		return scopeSelection{}, invalidScopeError("invalid station_code")
	}
	selection := scopeSelection{Scope: scope, StationCode: stationRaw}
	if scope == fleet.ScopeAll {
		if stationRaw != "" {
			selection.StationCodes = []string{stationRaw}
		}
		return selection, nil
	}
	if catalog == nil {
		return scopeSelection{}, fleetUnavailableError()
	}
	if stationRaw != "" {
		stationScope, ok := catalog.ScopeOf(stationRaw)
		if !ok || stationScope != scope {
			return scopeSelection{}, invalidScopeError("station_code does not match scope")
		}
		selection.StationCodes = []string{stationRaw}
		return selection, nil
	}
	selection.StationCodes = catalog.CodesForScope(scope)
	return selection, nil
}

func (s *Server) scopeSelection(c *gin.Context) (scopeSelection, bool) {
	selection, err := resolveScopeFilter(s.fleetCatalog, c.Query("scope"), c.Query("station_code"))
	if err != nil {
		fail(c, err.status, err.code, err.message)
		return scopeSelection{}, false
	}
	return selection, true
}

// applyToDevices 在 devices 表查询上应用场站范围。
func (selection scopeSelection) applyToDevices(query *gorm.DB) *gorm.DB {
	if len(selection.StationCodes) == 0 {
		return query
	}
	return query.Where("station_code IN ?", selection.StationCodes)
}

// applyToAlarms 通过有效设备子查询过滤告警，避免改动告警表结构。
func (selection scopeSelection) applyToAlarms(db *gorm.DB, query *gorm.DB) *gorm.DB {
	if len(selection.StationCodes) == 0 {
		return query
	}
	devices := db.Model(&model.Device{}).Select("id").Where("deleted_at IS NULL").Where("station_code IN ?", selection.StationCodes)
	return query.Where("device_id IN (?)", devices)
}

// stationRow 是 /stations 的固定响应字段。
type stationRow struct {
	StationCode           string `json:"station_code"`
	StationName           string `json:"station_name"`
	Scope                 string `json:"scope"`
	Kind                  string `json:"kind"`
	ExpectedDeviceCount   int    `json:"expected_device_count"`
	RegisteredDeviceCount int64  `json:"registered_device_count"`
}

// stationRows 按清单顺序构建场站列表；未导入设备的场站仍返回且 registered=0。
func stationRows(catalog *fleet.Catalog, scope string, registered map[string]int64) []stationRow {
	rows := make([]stationRow, 0, len(catalog.Stations))
	for _, station := range catalog.StationsForScope(scope) {
		rows = append(rows, stationRow{
			StationCode:           station.Code,
			StationName:           station.Name,
			Scope:                 station.Scope,
			Kind:                  station.Kind,
			ExpectedDeviceCount:   station.ExpectedDevices,
			RegisteredDeviceCount: registered[station.Code],
		})
	}
	return rows
}

// listStations 返回清单中的场站及有效设备台账的注册数量。
func (s *Server) listStations(c *gin.Context) {
	if s.fleetCatalog == nil {
		fail(c, http.StatusServiceUnavailable, fleetUnavailableCode, "fleet configuration unavailable")
		return
	}
	scope, err := fleet.NormalizeScope(c.Query("scope"))
	if err != nil {
		fail(c, http.StatusBadRequest, 40001, "invalid scope")
		return
	}
	var counts []struct {
		StationCode string
		Total       int64
	}
	if err := s.db.Model(&model.Device{}).Select("station_code, count(*) AS total").
		Where("deleted_at IS NULL").Group("station_code").Scan(&counts).Error; err != nil {
		fail(c, http.StatusInternalServerError, 50000, "station query failed")
		return
	}
	registered := make(map[string]int64, len(counts))
	for _, count := range counts {
		registered[count.StationCode] = count.Total
	}
	rows := stationRows(s.fleetCatalog, scope, registered)
	ok(c, gin.H{"scope": scope, "list": rows, "total": len(rows)})
}
