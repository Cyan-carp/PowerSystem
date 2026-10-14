package api

import (
	"context"
	"strconv"
	"time"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm"
)

// Only these server-owned expressions can enter GROUP BY. Client input is never SQL.
var alarmDimensions = map[string]string{
	"device": "a.device_id", "metric": "a.metric", "level": "a.level", "status": "a.status",
}

type alarmStatisticsQuery struct {
	Group      string
	Start, End time.Time
	DeviceID   int64
	Status     string
}

func parseAlarmStatistics(c *gin.Context, current time.Time) (alarmStatisticsQuery, bool) {
	q := alarmStatisticsQuery{Group: c.DefaultQuery("group_by", "device"), End: current.UTC()}
	for key, values := range c.Request.URL.Query() {
		if (key != "group_by" && key != "start" && key != "end" && key != "device_id" && key != "status") || len(values) != 1 {
			return q, false
		}
	}
	if _, exists := alarmDimensions[q.Group]; !exists {
		return q, false
	}
	var err error
	if value, exists := c.GetQuery("end"); exists {
		q.End, err = time.Parse(time.RFC3339Nano, value)
		if err != nil {
			return q, false
		}
	}
	q.Start = q.End.AddDate(0, 0, -30)
	if value, exists := c.GetQuery("start"); exists {
		q.Start, err = time.Parse(time.RFC3339Nano, value)
		if err != nil {
			return q, false
		}
	}
	if !q.End.After(q.Start) || q.End.Sub(q.Start) > 366*24*time.Hour || q.End.After(current.Add(time.Minute)) {
		return q, false
	}
	if value, exists := c.GetQuery("device_id"); exists {
		q.DeviceID, err = strconv.ParseInt(value, 10, 64)
		if err != nil || q.DeviceID < 1 {
			return q, false
		}
	}
	if value, exists := c.GetQuery("status"); exists {
		q.Status = value
		if value != "unhandled" && value != "acked" && value != "recovered" {
			return q, false
		}
	}
	q.Start, q.End = q.Start.UTC(), q.End.UTC()
	return q, true
}

type alarmStatisticsGroup struct {
	Key        string `json:"key"`
	DeviceID   *int64 `json:"device_id,omitempty"`
	DeviceCode string `json:"device_code,omitempty"`
	Count      int64  `json:"count"`
}

func aggregateAlarms(db *gorm.DB, q alarmStatisticsQuery) ([]alarmStatisticsGroup, int64, error) {
	dimension := alarmDimensions[q.Group]
	selection := dimension + " AS key, COUNT(*) AS count"
	grouping := dimension
	if q.Group == "device" {
		selection = "CAST(a.device_id AS TEXT) AS key, a.device_id, d.device_code, COUNT(*) AS count"
		grouping += ", d.device_code"
	}
	query := db.Table("alarm_records AS a").Joins("JOIN devices AS d ON d.id = a.device_id AND d.deleted_at IS NULL").
		Where("a.triggered_at >= ? AND a.triggered_at < ?", q.Start, q.End)
	if q.DeviceID > 0 {
		query = query.Where("a.device_id = ?", q.DeviceID)
	}
	if q.Status != "" {
		query = query.Where("a.status = ?", q.Status)
	}
	groups := make([]alarmStatisticsGroup, 0)
	// One aggregate statement gives a consistent snapshot and an unpaginated result.
	if err := query.Select(selection).Group(grouping).Order("count DESC, key ASC").Scan(&groups).Error; err != nil {
		return nil, 0, err
	}
	var total int64
	for _, group := range groups {
		total += group.Count
	}
	return groups, total, nil
}

func (s *Server) alarmStatistics(c *gin.Context) {
	q, valid := parseAlarmStatistics(c, time.Now())
	if !valid {
		fail(c, 400, 40001, "invalid alarm statistics query")
		return
	}
	ctx, cancel := context.WithTimeout(c.Request.Context(), 4*time.Second)
	defer cancel()
	groups, total, err := aggregateAlarms(s.db.WithContext(ctx), q)
	if err != nil {
		fail(c, 503, 50301, "alarm statistics unavailable")
		return
	}
	ok(c, gin.H{"group_by": q.Group, "start": q.Start, "end": q.End, "total_events": total,
		"groups": groups, "complete": true, "counting_basis": "triggered_at events in [start,end); includes recovered and unrecovered unless status filtered",
		"status_filter": q.Status, "device_id": q.DeviceID, "device_scope": "non-deleted visible devices"})
}
