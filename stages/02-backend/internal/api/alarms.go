package api

import (
	"errors"
	"math"
	"net/http"
	"strconv"
	"time"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm"

	"powersystem/backend/internal/model"
	"powersystem/backend/internal/service"
	"powersystem/backend/internal/telemetry"
)

type ruleInput struct {
	DeviceID  int64   `json:"device_id"`
	Metric    string  `json:"metric"`
	Operator  string  `json:"operator"`
	Threshold float64 `json:"threshold"`
	Level     string  `json:"level"`
	Enabled   *bool   `json:"enabled"`
}

func validRule(in ruleInput) bool {
	return in.DeviceID > 0 && telemetry.Metrics[in.Metric] && (in.Operator == ">" || in.Operator == ">=" || in.Operator == "<" || in.Operator == "<=") && !math.IsNaN(in.Threshold) && !math.IsInf(in.Threshold, 0) && (in.Level == "urgent" || in.Level == "major" || in.Level == "minor")
}

// nextVersion bumps a rule version like "v1" to "v2"; unknown versions restart at "v1".
func nextVersion(version string) string {
	if len(version) > 1 && version[0] == 'v' {
		if n, err := strconv.Atoi(version[1:]); err == nil && n > 0 {
			return "v" + strconv.Itoa(n+1)
		}
	}
	return "v1"
}
func (s *Server) listRules(c *gin.Context) {
	p, size, valid := page(c)
	if !valid {
		return
	}
	query := s.requestDB(c).Model(&model.AlarmRule{})
	if v := c.Query("device_id"); v != "" {
		query = query.Where("device_id=?", v)
	}
	var total int64
	if query.Count(&total).Error != nil {
		fail(c, 500, 50000, "rule query failed")
		return
	}
	var rules []model.AlarmRule
	if query.Order("id ASC").Offset((p-1)*size).Limit(size).Find(&rules).Error != nil {
		fail(c, 500, 50000, "rule query failed")
		return
	}
	for i := range rules {
		rules[i].Unit = metricUnit(rules[i].Metric)
	}
	ok(c, list(rules, p, size, total))
}

func metricUnit(metric string) string {
	return map[string]string{"voltage": "V", "current": "A", "temperature": "°C", "power": "kW"}[metric]
}
func (s *Server) createRule(c *gin.Context) {
	var in ruleInput
	if c.ShouldBindJSON(&in) != nil || !validRule(in) {
		fail(c, 400, 40001, "invalid rule")
		return
	}
	if _, err := s.findDevice(in.DeviceID); err != nil {
		fail(c, 404, 40401, "device not found")
		return
	}
	enabled := true
	if in.Enabled != nil {
		enabled = *in.Enabled
	}
	rule := model.AlarmRule{DeviceID: in.DeviceID, Metric: in.Metric, Operator: in.Operator, Threshold: in.Threshold, Level: in.Level, Enabled: enabled, Version: "v1"}
	if s.requestDB(c).Create(&rule).Error != nil {
		fail(c, 409, 40001, "rule already exists for device and metric")
		return
	}
	c.JSON(http.StatusCreated, response{0, "success", rule})
}
func (s *Server) getRule(c *gin.Context) {
	ruleID, valid := id(c)
	if !valid {
		return
	}
	var rule model.AlarmRule
	if s.requestDB(c).First(&rule, ruleID).Error != nil {
		fail(c, 404, 40401, "rule not found")
		return
	}
	ok(c, rule)
}
func (s *Server) updateRule(c *gin.Context) {
	ruleID, valid := id(c)
	if !valid {
		return
	}
	var rule model.AlarmRule
	if s.requestDB(c).First(&rule, ruleID).Error != nil {
		fail(c, 404, 40401, "rule not found")
		return
	}
	var in ruleInput
	if c.ShouldBindJSON(&in) != nil || !validRule(in) || in.DeviceID != rule.DeviceID || in.Metric != rule.Metric {
		fail(c, 400, 40001, "device and metric cannot change")
		return
	}
	rule.Operator = in.Operator
	rule.Threshold = in.Threshold
	rule.Level = in.Level
	rule.Version = nextVersion(rule.Version)
	if in.Enabled != nil {
		rule.Enabled = *in.Enabled
	}
	if err := s.requestDB(c).Transaction(func(tx *gorm.DB) error {
		if err := tx.Save(&rule).Error; err != nil {
			return err
		}
		if !rule.Enabled {
			now := time.Now().UTC()
			return s.closeRuleAlarms(tx, rule.DeviceID, rule.Metric, now, "rule_disabled")
		}
		return nil
	}); err != nil {
		fail(c, 500, 50000, "rule update failed")
		return
	}
	ok(c, rule)
}
func (s *Server) deleteRule(c *gin.Context) {
	ruleID, valid := id(c)
	if !valid {
		return
	}
	var rule model.AlarmRule
	if s.requestDB(c).First(&rule, ruleID).Error != nil {
		fail(c, 404, 40401, "rule not found")
		return
	}
	if err := s.requestDB(c).Transaction(func(tx *gorm.DB) error {
		if err := tx.Delete(&model.AlarmRule{}, ruleID).Error; err != nil {
			return err
		}
		now := time.Now().UTC()
		return s.closeRuleAlarms(tx, rule.DeviceID, rule.Metric, now, "rule_deleted")
	}); err != nil {
		fail(c, 500, 50000, "rule delete failed")
		return
	}
	ok(c, gin.H{"id": ruleID, "deleted": true})
}
func (s *Server) closeRuleAlarms(tx *gorm.DB, deviceID int64, metric string, now time.Time, reason string) error {
	var records []model.AlarmRecord
	if err := tx.Where("device_id=? AND metric=? AND recovered_at IS NULL", deviceID, metric).Find(&records).Error; err != nil {
		return err
	}
	if len(records) == 0 {
		return nil
	}
	var device model.Device
	if err := tx.First(&device, deviceID).Error; err != nil {
		return err
	}
	for _, record := range records {
		if err := tx.Model(&record).Updates(map[string]any{"status": "recovered", "recovered_at": now, "close_reason": reason, "updated_at": now}).Error; err != nil {
			return err
		}
		record.RecoveredAt = &now
		record.CloseReason = &reason
		if err := service.QueueAlarmNotification(tx, device, record, "closed"); err != nil {
			return err
		}
	}
	return nil
}
func (s *Server) listAlarms(c *gin.Context) {
	p, size, valid := page(c)
	if !valid {
		return
	}
	selection, valid := s.scopeSelection(c)
	if !valid {
		return
	}
	query := s.requestDB(c).Model(&model.AlarmRecord{}).Where("archived = FALSE")
	query = selection.applyToAlarms(s.requestDB(c), query)
	if origin := c.Query("origin"); origin != "" {
		if origin != "normal" && origin != "simulation" {
			fail(c, 400, 40001, "invalid origin")
			return
		}
		query = query.Where("origin=?", origin)
	}
	if device := c.Query("device_id"); device != "" {
		deviceID, err := strconv.ParseInt(device, 10, 64)
		if err != nil || deviceID < 1 {
			fail(c, 400, 40001, "invalid device_id")
			return
		}
		query = query.Where("device_id=?", deviceID)
	}
	var start, end time.Time
	for _, filter := range []struct {
		name       string
		target     *time.Time
		expression string
	}{
		{"start", &start, "triggered_at >= ?"}, {"end", &end, "triggered_at <= ?"},
	} {
		if raw := c.Query(filter.name); raw != "" {
			parsed, err := time.Parse(time.RFC3339, raw)
			if err != nil {
				fail(c, 400, 40001, "start/end require RFC3339 timestamps")
				return
			}
			*filter.target = parsed
			query = query.Where(filter.expression, parsed)
		}
	}
	if !start.IsZero() && !end.IsZero() && !end.After(start) {
		fail(c, 400, 40001, "end must be after start")
		return
	}
	if status := c.Query("status"); status != "" {
		if status != "unhandled" && status != "acked" && status != "recovered" {
			fail(c, 400, 40001, "invalid status")
			return
		}
		query = query.Where("status=?", status)
	}
	if level := c.Query("level"); level != "" {
		if level != "urgent" && level != "major" && level != "minor" {
			fail(c, 400, 40001, "invalid level")
			return
		}
		query = query.Where("level=?", level)
	}
	var total int64
	if query.Count(&total).Error != nil {
		fail(c, 500, 50000, "alarm query failed")
		return
	}
	var alarms []model.AlarmRecord
	if query.Order("triggered_at DESC,id DESC").Offset((p-1)*size).Limit(size).Find(&alarms).Error != nil {
		fail(c, 500, 50000, "alarm query failed")
		return
	}
	ok(c, list(alarms, p, size, total))
}
func (s *Server) getAlarm(c *gin.Context) {
	alarmID, valid := id(c)
	if !valid {
		return
	}
	var alarm model.AlarmRecord
	if s.requestDB(c).First(&alarm, alarmID).Error != nil {
		fail(c, 404, 40401, "alarm not found")
		return
	}
	ok(c, alarm)
}
func (s *Server) ackAlarm(c *gin.Context) {
	alarmID, valid := id(c)
	if !valid {
		return
	}
	now := time.Now().UTC()
	userID := c.GetInt64("user_id")
	var alarm model.AlarmRecord
	errConflict := service.ErrAlarmAckConflict
	err := s.requestDB(c).Transaction(func(tx *gorm.DB) error {
		var err error
		alarm, err = service.AcknowledgeAlarm(tx, alarmID, userID, now)
		return err
	})
	if errors.Is(err, errConflict) {
		fail(c, 409, 40001, "alarm is already acknowledged or recovered")
		return
	}
	if err != nil {
		fail(c, 500, 50000, "ack failed")
		return
	}
	s.broadcastAfterWrite(c, gin.H{"type": "alarm_acked", "data": alarm})
	ok(c, alarm)
}

func (s *Server) archiveAlarm(c *gin.Context) {
	alarmID, valid := id(c)
	if !valid {
		return
	}
	result := s.requestDB(c).Model(&model.AlarmRecord{}).Where("id=?", alarmID).
		Updates(map[string]any{"archived": true, "updated_at": time.Now().UTC()})
	if result.Error != nil {
		fail(c, 500, 50000, "archive failed")
		return
	}
	if result.RowsAffected == 0 {
		fail(c, 404, 40401, "alarm not found")
		return
	}
	var alarm model.AlarmRecord
	if s.requestDB(c).First(&alarm, alarmID).Error != nil {
		fail(c, 500, 50000, "archive failed")
		return
	}
	s.broadcastAfterWrite(c, gin.H{"type": "alarm_archived", "data": alarm})
	ok(c, alarm)
}

// archiveBatchInput 是批量归档的请求体；只接受正整数告警编号。
type archiveBatchInput struct {
	IDs []int64 `json:"ids"`
}

// archiveAlarms 批量更新归档标记，只更新标记，不删除告警事实、时间或规则快照。
// 已归档的编号保持已归档，不重复计数；不存在的编号在 failed 中返回，不中断其余编号。
func (s *Server) archiveAlarms(c *gin.Context) {
	var in archiveBatchInput
	if err := c.ShouldBindJSON(&in); err != nil {
		fail(c, http.StatusBadRequest, 40000, "请求格式不正确")
		return
	}
	ids := make([]int64, 0, len(in.IDs))
	seen := make(map[int64]bool, len(in.IDs))
	for _, alarmID := range in.IDs {
		if alarmID <= 0 || seen[alarmID] {
			continue
		}
		seen[alarmID] = true
		ids = append(ids, alarmID)
	}
	if len(ids) == 0 {
		fail(c, http.StatusBadRequest, 40000, "未提供告警编号")
		return
	}
	if len(ids) > 200 {
		fail(c, http.StatusBadRequest, 40000, "单次最多归档 200 条告警")
		return
	}
	now := time.Now().UTC()
	if err := s.requestDB(c).Model(&model.AlarmRecord{}).Where("id IN ?", ids).Where("archived = FALSE").
		Updates(map[string]any{"archived": true, "updated_at": now}).Error; err != nil {
		fail(c, http.StatusInternalServerError, 50000, "archive failed")
		return
	}
	var archivedIDs []int64
	if err := s.requestDB(c).Model(&model.AlarmRecord{}).Where("id IN ?", ids).Where("archived = TRUE").
		Pluck("id", &archivedIDs).Error; err != nil {
		fail(c, http.StatusInternalServerError, 50000, "archive failed")
		return
	}
	done := make(map[int64]bool, len(archivedIDs))
	for _, alarmID := range archivedIDs {
		done[alarmID] = true
	}
	failed := make([]int64, 0)
	for _, alarmID := range ids {
		if !done[alarmID] {
			failed = append(failed, alarmID)
		}
	}
	if len(archivedIDs) > 0 {
		s.broadcastAfterWrite(c, gin.H{"type": "alarm_archived", "data": gin.H{"ids": archivedIDs}})
	}
	ok(c, gin.H{"archived": len(archivedIDs), "failed": failed, "updated_at": now})
}
