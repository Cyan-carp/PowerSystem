package api

import (
	"context"
	"time"

	"github.com/gin-gonic/gin"

	"powersystem/backend/internal/model"
	"powersystem/backend/internal/telemetry"
)

type telemetryLinkRow struct {
	DeviceID          int64      `json:"device_id"`
	StationCode       string     `json:"-"`
	ReceivedAt        *time.Time `json:"received_at"`
	ConfirmedAt       *time.Time `json:"confirmed_at"`
	ConfirmedReceived *time.Time `json:"-"`
	DelaySeconds      *int64     `json:"delay_seconds"`
	State             string     `json:"state"`
}

func receiptFresh(now time.Time, received *time.Time) bool {
	return received != nil && !received.After(now) && telemetry.Freshness(now, *received, now.UnixMilli()) == "fresh"
}

func linkDelay(row telemetryLinkRow, now time.Time) *int64 {
	if row.ReceivedAt == nil || row.ConfirmedReceived == nil || row.ReceivedAt.After(now) {
		return nil
	}
	seconds := int64(row.ReceivedAt.Sub(*row.ConfirmedReceived) / time.Second)
	if seconds < 0 {
		return nil
	}
	return &seconds
}

func classifyLink(now time.Time, row telemetryLinkRow, peerFresh, platformFresh bool, healthState string) string {
	if receiptFresh(now, row.ReceivedAt) {
		if row.ConfirmedReceived == nil || row.ConfirmedReceived.After(now) {
			return "unknown"
		}
		if receiptFresh(now, row.ConfirmedReceived) {
			return "current"
		}
		return "processing_delay"
	}
	if row.ReceivedAt == nil || row.ReceivedAt.After(now) || !platformFresh {
		return "unknown"
	}
	if peerFresh && healthState == "offline" {
		return "offline"
	}
	return "no_new_reports"
}

// One indexed latest-receipt lookup per active device. Never count inbox rows at request time.
func (s *Server) telemetryLinks(ctx context.Context) ([]telemetryLinkRow, error) {
	rows := make([]telemetryLinkRow, 0)
	err := s.db.WithContext(ctx).Raw(`SELECT d.id AS device_id, d.station_code, latest.received_at,
  snap.confirmed_at, accepted.received_at AS confirmed_received
FROM devices d
LEFT JOIN LATERAL (SELECT received_at FROM telemetry_inbox WHERE device_id=d.id ORDER BY id DESC LIMIT 1) latest ON true
LEFT JOIN telemetry_snapshots snap ON snap.device_id=d.id
LEFT JOIN telemetry_inbox accepted ON accepted.id=snap.inbox_id
WHERE d.deleted_at IS NULL`).Scan(&rows).Error
	return rows, err
}

func (s *Server) platformLinkStatus(c *gin.Context) {
	now := time.Now().UTC()
	result := s.inboxOperationsSnapshot(now)
	result["scope"] = "platform"
	result["received_at"], result["delay_seconds"] = nil, nil
	result["state"] = "unknown"
	ctx, cancel := context.WithTimeout(c.Request.Context(), 2*time.Second)
	defer cancel()
	rows, err := s.telemetryLinks(ctx)
	if err == nil {
		now = time.Now().UTC()
		var latest *time.Time
		var delay *int64
		state := "unknown"
		for _, row := range rows {
			if row.ReceivedAt != nil && !row.ReceivedAt.After(now) && (latest == nil || row.ReceivedAt.After(*latest)) {
				latest = row.ReceivedAt
			}
			if !receiptFresh(now, row.ReceivedAt) {
				continue
			}
			if value := linkDelay(row, now); value != nil && (delay == nil || *value > *delay) {
				delay = value
			}
			linkState := classifyLink(now, row, false, true, "")
			if linkState == "processing_delay" || state == "unknown" && linkState == "current" {
				state = linkState
			}
		}
		result["received_at"], result["delay_seconds"], result["state"] = latest, delay, state
	}
	ok(c, result)
}

type linkCoverage struct {
	Day       string `json:"day"`
	CoveredMS *int64 `json:"covered_ms"`
	GapCount  *int64 `json:"gap_count"`
	MaxGapMS  *int64 `json:"max_gap_ms"`
}

func (s *Server) deviceLinkStatus(c *gin.Context) {
	deviceID, valid := id(c)
	if !valid {
		return
	}
	now := time.Now().UTC()
	ctx, cancel := context.WithTimeout(c.Request.Context(), 3*time.Second)
	defer cancel()
	rows, err := s.telemetryLinks(ctx)
	if err != nil {
		fail(c, 503, 50301, "暂时无法读取设备链路")
		return
	}
	now = time.Now().UTC()
	var row telemetryLinkRow
	for _, entry := range rows {
		if entry.DeviceID == deviceID {
			row = entry
			break
		}
	}
	if row.DeviceID == 0 {
		fail(c, 404, 40401, "设备不存在")
		return
	}
	platformFresh, peerFresh := false, false
	for _, entry := range rows {
		if entry.DeviceID != deviceID && receiptFresh(now, entry.ReceivedAt) {
			platformFresh = true
			peerFresh = peerFresh || entry.StationCode == row.StationCode
		}
	}
	healthState := "unknown"
	if peerFresh && s.redis != nil && !receiptFresh(now, row.ReceivedAt) {
		healthState = s.scoreDevice(ctx, model.Device{ID: deviceID}, "").State
	}
	row.State = classifyLink(now, row, peerFresh, platformFresh, healthState)
	if receiptFresh(now, row.ReceivedAt) {
		row.DelaySeconds = linkDelay(row, now)
	}
	// The existing daily energy provenance uses Shanghai dates and tracks dirty rollups.
	today := now.In(time.FixedZone("CST", 8*3600))
	start, end := today.AddDate(0, 0, -7).Format("2006-01-02"), today.Format("2006-01-02")
	var verified []linkCoverage
	err = s.db.WithContext(ctx).Raw(`SELECT CAST(e.day AS TEXT) AS day, e.covered_ms,e.gap_count,e.max_gap_ms
FROM energy_daily e WHERE device_id=? AND day>=? AND day<? AND source_quality='verified'
AND NOT EXISTS (SELECT 1 FROM energy_dirty_days q WHERE q.device_id=e.device_id AND q.day=e.day)`, deviceID, start, end).Scan(&verified).Error
	coverage := make([]linkCoverage, 0, 7)
	for day := -7; day < 0; day++ {
		item := linkCoverage{Day: today.AddDate(0, 0, day).Format("2006-01-02")}
		for _, candidate := range verified {
			if candidate.Day == item.Day && err == nil {
				item = candidate
				break
			}
		}
		coverage = append(coverage, item)
	}
	ok(c, gin.H{"link": row, "coverage": coverage, "observed_at": now})
}
