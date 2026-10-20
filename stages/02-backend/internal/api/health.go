package api

import (
	"context"
	"encoding/json"
	"errors"
	"sort"
	"strconv"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/redis/go-redis/v9"

	"powersystem/backend/internal/model"
)

// rule-health-v1：当前时点的规则健康评分。离线或遥测故障为 0 分；在线非故障
// 设备按最高活动告警级别取紧急 40、重要 70、一般 90、无活动告警 100 分。
const healthRuleVersion = "rule-health-v1"

// deviceHealth 是单设备评分结果；Score 为 nil 表示未知（数据不足，不参与排行）。
type deviceHealth struct {
	Rank        *int     `json:"rank"`
	DeviceID    int64    `json:"device_id"`
	DeviceCode  string   `json:"device_code"`
	Name        string   `json:"name"`
	StationCode string   `json:"station_code"`
	Score       *float64 `json:"score"`
	State       string   `json:"state"`
	ActiveLevel string   `json:"active_level,omitempty"`
	Status      *int     `json:"status,omitempty"`
	SampledAt   *int64   `json:"sampled_at_ms,omitempty"`
	ReceivedAt  *string  `json:"received_at,omitempty"`
	MatchedBy   string   `json:"matched_by"`
	RuleVersion string   `json:"rule_version"`
}

// scoreDevice 分别验证接收与采样新鲜度，把「未知」与「离线 0 分」分开。
func (s *Server) scoreDevice(ctx context.Context, d model.Device, activeLevel string) deviceHealth {
	h := deviceHealth{DeviceID: d.ID, DeviceCode: d.DeviceCode, Name: d.Name,
		StationCode: d.StationCode, ActiveLevel: activeLevel, RuleVersion: healthRuleVersion, State: "unknown", MatchedBy: "no valid sample"}
	raw, err := s.redis.Get(ctx, "device:"+strconv.FormatInt(d.ID, 10)+":latest").Bytes()
	if errors.Is(err, redis.Nil) {
		// 从未取得合法样本，评分未知，不以 0 分掩盖数据不足。
		return h
	}
	if err != nil {
		h.MatchedBy = "latest cache unavailable"
		return h
	}
	return scoreLatestHealth(h, raw, activeLevel, time.Now())
}

func scoreLatestHealth(h deviceHealth, raw []byte, activeLevel string, now time.Time) deviceHealth {
	var v struct {
		ReceivedAt *time.Time `json:"received_at"`
		TS         *int64     `json:"ts_ms"`
		Status     *int       `json:"status"`
	}
	if json.Unmarshal(raw, &v) != nil {
		h.MatchedBy = "latest payload invalid"
		return h
	}
	if v.ReceivedAt == nil || v.TS == nil || v.Status == nil || *v.Status < 0 || *v.Status > 2 {
		h.MatchedBy = "latest payload incomplete or invalid"
		return h
	}
	h.Status = v.Status
	h.SampledAt = v.TS
	rcv := v.ReceivedAt.UTC().Format(time.RFC3339Nano)
	h.ReceivedAt = &rcv
	if v.ReceivedAt.IsZero() || v.ReceivedAt.After(now) {
		h.MatchedBy = "reception time anomaly"
		return h
	}
	// 保留既有 60 秒采样时钟容差；缺失或异常时间不能证明连接离线。
	sampled := time.UnixMilli(*v.TS)
	if *v.TS <= 0 || sampled.After(now.Add(60*time.Second)) || sampled.After(v.ReceivedAt.Add(60*time.Second)) {
		h.MatchedBy = "sample time anomaly"
		return h
	}
	if now.Sub(*v.ReceivedAt) > 15*time.Second {
		// 合法旧数据可证明连接已离线；离线记 0 分但不解释为已证实设备故障。
		score := 0.0
		h.Score = &score
		h.State = "offline"
		h.MatchedBy = "stale reception (>15s)"
		return h
	}
	// 连接新鲜不等于采样新鲜；刚收到旧样本时必须保留未知。
	if now.Sub(sampled) > 15*time.Second {
		h.MatchedBy = "stale sample (>15s)"
		return h
	}
	if *v.Status == 2 {
		score := 0.0
		h.Score = &score
		h.State = "fault"
		h.MatchedBy = "status=2 fault"
		return h
	}
	h.State = "online"
	h.ActiveLevel = activeLevel
	score := 100.0
	switch activeLevel {
	case "urgent":
		score = 40
	case "major":
		score = 70
	case "minor":
		score = 90
	}
	h.Score = &score
	h.MatchedBy = "highest active alarm level"
	if activeLevel == "" {
		h.MatchedBy = "online with no active alarm"
	}
	return h
}

// activeLevelByDevice returns the highest active alarm level per device id.
func (s *Server) activeLevelByDevice() (map[int64]string, error) {
	var levels []struct {
		DeviceID int64
		Rank     int
	}
	if err := s.db.Raw(`SELECT device_id,MAX(CASE level WHEN 'urgent' THEN 3 WHEN 'major' THEN 2 WHEN 'minor' THEN 1 ELSE 0 END) AS rank
FROM alarm_records WHERE recovered_at IS NULL GROUP BY device_id`).Scan(&levels).Error; err != nil {
		return nil, err
	}
	out := make(map[int64]string, len(levels))
	for _, level := range levels {
		out[level.DeviceID] = map[int]string{1: "minor", 2: "major", 3: "urgent"}[level.Rank]
	}
	return out, nil
}

func rankScores(items []deviceHealth) []deviceHealth {
	sort.SliceStable(items, func(i, j int) bool {
		// 未知设备排在已知设备之后；已知设备按分数从高到低。
		if items[i].Score == nil && items[j].Score == nil {
			return items[i].DeviceCode < items[j].DeviceCode
		}
		if items[i].Score == nil {
			return false
		}
		if items[j].Score == nil {
			return true
		}
		if *items[i].Score != *items[j].Score {
			return *items[i].Score > *items[j].Score
		}
		return items[i].DeviceCode < items[j].DeviceCode
	})
	for i := range items {
		items[i].Rank = nil
		if items[i].Score == nil {
			continue
		}
		rank := i + 1
		if i > 0 && items[i-1].Score != nil && *items[i-1].Score == *items[i].Score {
			rank = *items[i-1].Rank
		}
		items[i].Rank = &rank
	}
	return items
}

// stationHealth 场站汇总：有效设备分数等权平均；存在未知设备时为暂定分，不参加正式排行。
type stationHealth struct {
	Rank         *int     `json:"rank"`
	StationCode  string   `json:"station_code"`
	Score        *float64 `json:"score"`
	Provisional  bool     `json:"provisional"`
	DeviceTotal  int      `json:"device_total"`
	ValidCount   int      `json:"valid_count"`
	UnknownCount int      `json:"unknown_count"`
	RuleVersion  string   `json:"rule_version"`
}

func (s *Server) healthDevices(c *gin.Context) {
	devices, err := s.findDevices()
	if err != nil {
		fail(c, 500, 50000, "health scoring failed")
		return
	}
	items, err := s.scoreDevices(c.Request.Context(), devices)
	if err != nil {
		fail(c, 500, 50000, "health scoring failed")
		return
	}
	ok(c, gin.H{"rule_version": healthRuleVersion, "computed_at": time.Now().UTC().Format(time.RFC3339Nano),
		"devices": items, "stations": aggregateHealth(items), "complete": allHealthKnown(items), "basis": "current ledger; unknown excluded; ties share competition rank; offline does not prove a hardware fault"})
}

func (s *Server) scoreDevices(ctx context.Context, devices []model.Device) ([]deviceHealth, error) {
	levels, err := s.activeLevelByDevice()
	if err != nil {
		return nil, err
	}
	items := make([]deviceHealth, 0, len(devices))
	for _, d := range devices {
		items = append(items, s.scoreDevice(ctx, d, levels[d.ID]))
	}
	return rankScores(items), nil
}

func allHealthKnown(items []deviceHealth) bool {
	for _, h := range items {
		if h.Score == nil {
			return false
		}
	}
	return true
}

func healthAverage(items []deviceHealth) (*float64, int) {
	sum, count := 0.0, 0
	for _, h := range items {
		if h.Score != nil {
			sum += *h.Score
			count++
		}
	}
	if count == 0 {
		return nil, 0
	}
	avg := sum / float64(count)
	return &avg, count
}

func (s *Server) healthStations(c *gin.Context) {
	devices, err := s.findDevices()
	if err != nil {
		fail(c, 500, 50000, "station health failed")
		return
	}
	items, err := s.scoreDevices(c.Request.Context(), devices)
	if err != nil {
		fail(c, 500, 50000, "station health failed")
		return
	}
	ok(c, gin.H{"rule_version": healthRuleVersion, "computed_at": time.Now().UTC().Format(time.RFC3339Nano), "stations": aggregateHealth(items), "complete": allHealthKnown(items), "basis": "current ledger; equal-weight valid scores; provisional stations have no formal rank"})
}

func aggregateHealth(items []deviceHealth) []stationHealth {
	byStation := make(map[string][]deviceHealth)
	for _, h := range items {
		byStation[h.StationCode] = append(byStation[h.StationCode], h)
	}
	stations := make([]stationHealth, 0, len(byStation))
	for code, list := range byStation {
		st := stationHealth{StationCode: code, RuleVersion: healthRuleVersion, DeviceTotal: len(list)}
		sum, valid := 0.0, 0
		for _, h := range list {
			if h.Score == nil {
				st.UnknownCount++
				continue
			}
			sum += *h.Score
			valid++
		}
		st.ValidCount = valid
		if st.UnknownCount > 0 {
			st.Provisional = true
		}
		if valid > 0 {
			avg := sum / float64(valid)
			st.Score = &avg
		}
		stations = append(stations, st)
	}
	sort.SliceStable(stations, func(i, j int) bool {
		if stations[i].Provisional != stations[j].Provisional {
			return !stations[i].Provisional
		}
		if stations[i].Score == nil && stations[j].Score == nil {
			return stations[i].StationCode < stations[j].StationCode
		}
		if stations[i].Score == nil {
			return false
		}
		if stations[j].Score == nil {
			return true
		}
		if *stations[i].Score != *stations[j].Score {
			return *stations[i].Score > *stations[j].Score
		}
		return stations[i].StationCode < stations[j].StationCode
	})
	for i := range stations {
		if stations[i].Score == nil || stations[i].Provisional {
			continue
		}
		rank := i + 1
		if i > 0 && stations[i-1].Rank != nil && *stations[i-1].Score == *stations[i].Score {
			rank = *stations[i-1].Rank
		}
		stations[i].Rank = &rank
	}
	return stations
}
