// Package fleet 读取共享车队清单（fleet.json）。
//
// 清单是模拟器、台账导入和场站元数据的唯一来源；本包只读取，不生成、不复制第二份清单。
package fleet

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"strings"

	"powersystem/backend/internal/telemetry"
)

// SchemaVersion 是当前支持的清单版本。
const SchemaVersion = 1

// 统计范围取值。省略表示 all。
const (
	ScopeAll      = "all"
	ScopeBusiness = "business"
	ScopeBaseline = "baseline"
)

// maxManifestBytes 限制清单读取体积，避免异常文件拖垮启动。
const maxManifestBytes = 1 << 20

// Station 是清单中的一个场站。ExpectedDevices 为 business 的 device_count
// 或 baseline 的 explicit 长度。
type Station struct {
	Code                string
	Name                string
	Scope               string
	Kind                string
	DevicePrefix        string
	ExpectedDevices     int
	RatedPowerKW        float64
	Profile             string
	DaylightMode        string
	DaylightLoopMinutes int
	GroupSize           int
	Explicit            []string
}

// Catalog 是校验通过的清单，保留文件中的场站顺序。
type Catalog struct {
	FleetID         string
	SamplingSeconds int
	Stations        []Station
}

// NormalizeScope 只接受 all/business/baseline，空字符串等价于 all。
func NormalizeScope(raw string) (string, error) {
	switch raw {
	case "", ScopeAll:
		return ScopeAll, nil
	case ScopeBusiness:
		return ScopeBusiness, nil
	case ScopeBaseline:
		return ScopeBaseline, nil
	default:
		return "", fmt.Errorf("invalid scope %q", raw)
	}
}

// Load 从路径读取并严格校验清单。
func Load(path string) (*Catalog, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	catalog, err := Parse(f)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	return catalog, nil
}

// Parse 严格解析清单：拒绝未知字段、缺失字段、重复编码、非法 scope 和无效设备数。
func Parse(r io.Reader) (*Catalog, error) {
	dec := json.NewDecoder(io.LimitReader(r, maxManifestBytes))
	dec.DisallowUnknownFields()
	var raw rawCatalog
	if err := dec.Decode(&raw); err != nil {
		return nil, fmt.Errorf("invalid fleet manifest: %w", err)
	}
	if err := ensureEOF(dec); err != nil {
		return nil, err
	}
	if raw.SchemaVersion == nil {
		return nil, errors.New("schema_version is required")
	}
	if *raw.SchemaVersion != SchemaVersion {
		return nil, fmt.Errorf("unsupported schema_version %d", *raw.SchemaVersion)
	}
	if strings.TrimSpace(raw.FleetID) == "" {
		return nil, errors.New("fleet_id is required")
	}
	if raw.SamplingSeconds == nil || *raw.SamplingSeconds <= 0 {
		return nil, errors.New("sampling_seconds must be a positive integer")
	}
	if len(raw.Stations) == 0 {
		return nil, errors.New("stations must not be empty")
	}

	catalog := &Catalog{FleetID: raw.FleetID, SamplingSeconds: *raw.SamplingSeconds}
	seenStations := make(map[string]bool, len(raw.Stations))
	seenDevices := make(map[string]bool)
	for index, item := range raw.Stations {
		station, err := item.validate(index, seenStations, seenDevices)
		if err != nil {
			return nil, err
		}
		catalog.Stations = append(catalog.Stations, station)
	}
	return catalog, nil
}

// Station 按编码查找场站。
func (c *Catalog) Station(code string) (Station, bool) {
	if c == nil {
		return Station{}, false
	}
	for _, station := range c.Stations {
		if station.Code == code {
			return station, true
		}
	}
	return Station{}, false
}

// ScopeOf 返回场站所属范围。
func (c *Catalog) ScopeOf(code string) (string, bool) {
	station, ok := c.Station(code)
	if !ok {
		return "", false
	}
	return station.Scope, true
}

// CodesForScope 按清单顺序返回该范围的场站编码；all 返回全部场站。
func (c *Catalog) CodesForScope(scope string) []string {
	if c == nil {
		return nil
	}
	codes := make([]string, 0, len(c.Stations))
	for _, station := range c.Stations {
		if scope == ScopeAll || station.Scope == scope {
			codes = append(codes, station.Code)
		}
	}
	return codes
}

// StationsForScope 按清单顺序返回该范围的场站；all 返回全部场站。
func (c *Catalog) StationsForScope(scope string) []Station {
	if c == nil {
		return nil
	}
	stations := make([]Station, 0, len(c.Stations))
	for _, station := range c.Stations {
		if scope == ScopeAll || station.Scope == scope {
			stations = append(stations, station)
		}
	}
	return stations
}

// ExpectedDevices 返回该范围的期望设备数；all 为全部场站之和。
func (c *Catalog) ExpectedDevices(scope string) int {
	total := 0
	for _, station := range c.StationsForScope(scope) {
		total += station.ExpectedDevices
	}
	return total
}

func ensureEOF(dec *json.Decoder) error {
	var extra any
	if err := dec.Decode(&extra); err != io.EOF {
		return errors.New("invalid fleet manifest: trailing JSON")
	}
	return nil
}

type rawCatalog struct {
	SchemaVersion   *int         `json:"schema_version"`
	FleetID         string       `json:"fleet_id"`
	SamplingSeconds *int         `json:"sampling_seconds"`
	Stations        []rawStation `json:"stations"`
}

type rawStation struct {
	StationCode         string   `json:"station_code"`
	StationName         string   `json:"station_name"`
	Scope               string   `json:"scope"`
	Kind                string   `json:"kind"`
	DevicePrefix        string   `json:"device_prefix"`
	DeviceCount         *int     `json:"device_count"`
	RatedPowerKW        *float64 `json:"rated_power_kw"`
	Profile             string   `json:"profile"`
	DaylightMode        string   `json:"daylight_mode"`
	DaylightLoopMinutes *int     `json:"daylight_loop_minutes"`
	GroupSize           *int     `json:"group_size"`
	Explicit            []string `json:"explicit"`
}

func (raw rawStation) validate(index int, seenStations, seenDevices map[string]bool) (Station, error) {
	where := fmt.Sprintf("stations[%d]", index)
	if !telemetry.Code.MatchString(raw.StationCode) {
		return Station{}, fmt.Errorf("%s: invalid station_code", where)
	}
	if seenStations[raw.StationCode] {
		return Station{}, fmt.Errorf("%s: duplicate station_code %q", where, raw.StationCode)
	}
	seenStations[raw.StationCode] = true
	if strings.TrimSpace(raw.StationName) == "" {
		return Station{}, fmt.Errorf("%s: station_name is required", where)
	}
	if strings.TrimSpace(raw.Kind) == "" {
		return Station{}, fmt.Errorf("%s: kind is required", where)
	}
	switch raw.DaylightMode {
	case "", "real", "loop":
	default:
		return Station{}, fmt.Errorf("%s: invalid daylight_mode %q", where, raw.DaylightMode)
	}
	if raw.DaylightMode == "loop" && (raw.DaylightLoopMinutes == nil || *raw.DaylightLoopMinutes <= 0) {
		return Station{}, fmt.Errorf("%s: daylight_loop_minutes must be positive for loop mode", where)
	}
	if raw.DaylightMode != "loop" && raw.DaylightLoopMinutes != nil {
		return Station{}, fmt.Errorf("%s: daylight_loop_minutes requires loop mode", where)
	}

	station := Station{
		Code:         raw.StationCode,
		Name:         raw.StationName,
		Scope:        raw.Scope,
		Kind:         raw.Kind,
		DaylightMode: raw.DaylightMode,
	}
	switch raw.Scope {
	case ScopeBusiness:
		if raw.Explicit != nil {
			return Station{}, fmt.Errorf("%s: business station must not declare explicit devices", where)
		}
		if !telemetry.Code.MatchString(raw.DevicePrefix) {
			return Station{}, fmt.Errorf("%s: invalid device_prefix", where)
		}
		if raw.DeviceCount == nil || *raw.DeviceCount <= 0 {
			return Station{}, fmt.Errorf("%s: device_count must be a positive integer", where)
		}
		if raw.RatedPowerKW == nil || *raw.RatedPowerKW <= 0 {
			return Station{}, fmt.Errorf("%s: rated_power_kw must be positive", where)
		}
		if strings.TrimSpace(raw.Profile) == "" {
			return Station{}, fmt.Errorf("%s: profile is required", where)
		}
		if raw.GroupSize != nil && *raw.GroupSize <= 0 {
			return Station{}, fmt.Errorf("%s: group_size must be positive", where)
		}
		station.DevicePrefix = raw.DevicePrefix
		station.ExpectedDevices = *raw.DeviceCount
		station.RatedPowerKW = *raw.RatedPowerKW
		station.Profile = raw.Profile
		if raw.GroupSize != nil {
			station.GroupSize = *raw.GroupSize
		}
	case ScopeBaseline:
		if raw.DeviceCount != nil || raw.DevicePrefix != "" || raw.RatedPowerKW != nil || raw.Profile != "" || raw.GroupSize != nil {
			return Station{}, fmt.Errorf("%s: baseline station must not declare business device fields", where)
		}
		if len(raw.Explicit) == 0 {
			return Station{}, fmt.Errorf("%s: explicit devices are required for baseline station", where)
		}
		for _, code := range raw.Explicit {
			if !telemetry.Code.MatchString(code) {
				return Station{}, fmt.Errorf("%s: invalid explicit device_code %q", where, code)
			}
			if seenDevices[code] {
				return Station{}, fmt.Errorf("%s: duplicate device_code %q", where, code)
			}
			seenDevices[code] = true
		}
		station.Explicit = append([]string(nil), raw.Explicit...)
		station.ExpectedDevices = len(raw.Explicit)
	default:
		return Station{}, fmt.Errorf("%s: scope must be business or baseline", where)
	}
	if raw.DaylightMode == "loop" {
		station.DaylightLoopMinutes = *raw.DaylightLoopMinutes
	}
	return station, nil
}
