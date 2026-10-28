package api

import (
	"github.com/gin-gonic/gin"
	"strconv"
	"time"
)

// Bounded audit projection. Historical suppression is an explicit alarm gap,
// independent from the realtime TD-confirmed snapshot and energy provenance.
func (s *Server) telemetryProgress(c *gin.Context) {
	device, valid := id(c)
	if !valid {
		return
	}
	if _, err := s.findDevice(device); err != nil {
		fail(c, 404, 40401, "device not found")
		return
	}
	type row struct {
		ID              int64      `json:"inbox_id,string"`
		TS              int64      `json:"ts_ms" gorm:"column:ts_ms"`
		ReceivedAt      time.Time  `json:"received_at"`
		ProcessedAt     *time.Time `json:"processed_at"`
		BusinessState   *string    `json:"business_state"`
		TDConfirmedAt   *time.Time `json:"td_confirmed_at"`
		AlarmEvaluation *string    `json:"alarm_evaluation"`
		FailureCode     *string    `json:"failure_code"`
	}
	var rows []row
	query := `SELECT id,ts_ms,received_at,processed_at,business_state,td_confirmed_at,alarm_evaluation,failure_code FROM telemetry_inbox WHERE device_id=?`
	args := []any{device}
	if raw := c.Query("before_id"); raw != "" {
		before, e := strconv.ParseInt(raw, 10, 64)
		if e != nil || before < 1 {
			fail(c, 400, 40001, "invalid before_id")
			return
		}
		query += " AND id<?"
		args = append(args, before)
	}
	query += " ORDER BY id DESC LIMIT 64"
	if err := s.requestDB(c).Raw(query, args...).Scan(&rows).Error; err != nil {
		fail(c, 503, 50301, "telemetry progress unavailable")
		return
	}
	for i := range rows {
		rows[i].ReceivedAt = rows[i].ReceivedAt.UTC()
		if rows[i].TDConfirmedAt != nil {
			at := rows[i].TDConfirmedAt.UTC()
			rows[i].TDConfirmedAt = &at
		}
		if rows[i].ProcessedAt != nil {
			at := rows[i].ProcessedAt.UTC()
			rows[i].ProcessedAt = &at
		}
	}
	var next any
	if len(rows) == 64 {
		next = strconv.FormatInt(rows[len(rows)-1].ID, 10)
	}
	ok(c, gin.H{"next_before_id": next, "device_id": device, "limit": 64, "window": "latest durable receipts; not full history", "rows": rows, "historical_alarm_policy": "suppressed; gaps retained; no external replay", "snapshot_business_commit": "not_asserted"})
}
