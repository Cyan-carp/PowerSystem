package api

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"powersystem/backend/internal/model"
	"powersystem/backend/internal/service"
)

// Only an explicitly marked server-local manual demo enters the model budget.
// A database error never falls through to an unexpected paid request.
func (s *Server) processFixedInterpretation(ctx context.Context, item interpretationTask) (bool, error) {
	stamp := time.Now().UTC().Format(time.RFC3339Nano)
	evidence := []eventEvidence{}
	var alarm model.AlarmRecord
	if item.AlarmID != nil {
		if err := s.db.WithContext(ctx).First(&alarm, *item.AlarmID).Error; err != nil {
			return true, err
		}
		if service.IsManualAPIDemo(alarm) && strings.HasPrefix(item.EventKey, service.ManualAPIDemoEventPrefix) {
			return false, nil
		}
		at := alarm.TriggeredAt.UTC().Format(time.RFC3339Nano)
		evidence = append(evidence, eventEvidence{ID: "E1", Tool: "get_alarm_detail", Status: "ok", Source: fmt.Sprintf("/api/v1/alarms/%d", alarm.ID), CollectedAt: stamp, DataTime: &at, Data: map[string]any{"id": alarm.ID, "device_id": alarm.DeviceID, "metric": alarm.Metric, "level": alarm.Level, "value": alarm.Value, "threshold": alarm.Threshold, "triggered_at": at, "origin": alarm.Origin}})
		var device model.Device
		if err := s.db.WithContext(ctx).First(&device, alarm.DeviceID).Error; err != nil {
			return true, err
		}
		evidence = append(evidence, eventEvidence{ID: "E2", Tool: "device", Status: "ok", Source: fmt.Sprintf("/api/v1/devices/%d", device.ID), CollectedAt: stamp, Data: map[string]any{"device_code": device.DeviceCode, "station_code": device.StationCode, "name": device.Name}})
	} else {
		var row struct{ Payload json.RawMessage }
		if err := s.db.WithContext(ctx).Raw("SELECT payload FROM agent_monitor_events WHERE event_key=?", item.EventKey).Scan(&row).Error; err != nil {
			return true, err
		}
		var data map[string]any
		if err := json.Unmarshal(row.Payload, &data); err != nil {
			return true, err
		}
		evidence = append(evidence, eventEvidence{ID: "E1", Tool: "monitor_event", Status: "ok", Source: "alertmanager/webhook", CollectedAt: stamp, Data: data})
	}
	result := fixedInterpretationResult(item, alarm, evidence)
	raw, err := json.Marshal(result)
	if err != nil {
		return true, err
	}
	evidenceRaw, err := json.Marshal(evidence)
	if err != nil {
		return true, err
	}
	r := s.db.WithContext(ctx).Exec(`UPDATE agent_interpretations SET task_status='completed',protocol_version=2,evidence=?::jsonb,result=?::jsonb,reason='',lease_until=NULL,lease_token=NULL,updated_at=now() WHERE id=? AND lease_token=? AND lease_until>now()`, string(evidenceRaw), string(raw), item.ID, item.LeaseToken)
	if r.Error != nil {
		return true, r.Error
	}
	if r.RowsAffected == 1 {
		s.broadcast(gin.H{"type": "interpretation_updated", "data": gin.H{"id": item.ID, "status": "completed", "updated_at": time.Now().UTC()}})
	}
	return true, nil
}

func fixedInterpretationResult(item interpretationTask, alarm model.AlarmRecord, evidence []eventEvidence) gin.H {
	text := "平台监控事件已记录。请核对服务状态、监控原文与最近变更。"
	steps := []string{"检查监控原文与关联服务健康状态。", "对照最近部署和资源变化，确认影响范围。"}
	if item.AlarmID != nil {
		metric := map[string]string{"temperature": "温度", "voltage": "电压", "current": "电流", "power": "功率", "ai_failure_risk": "预测风险", "simulation_scenario": "演示场景"}[alarm.Metric]
		if metric == "" {
			metric = "设备指标"
		}
		text = metric + "告警已按规则记录。请以触发时刻的读数和规则快照核对，固定模板不判断实际故障根因。"
		steps = []string{"核对触发读数、规则阈值与相邻设备同期数据。", "结合设备状态、现场复测与运维记录判断原因，按既有操作规程处置。", "读数恢复后核对告警恢复状态，持续异常时转人工排查。"}
		if alarm.Metric == "temperature" {
			steps[0] = "核对温度读数、环境条件与相邻设备温度，检查散热和传感器状态。"
		}
		if alarm.Metric == "voltage" {
			steps[0] = "核对电压采样、并网条件与相邻设备读数，检查保护和接线记录。"
		}
	}
	suggestions := []gin.H{}
	for _, step := range steps {
		suggestions = append(suggestions, gin.H{"text": step, "evidence_ids": []string{"E1"}, "equipment_operation": false})
	}
	return gin.H{"protocol_version": 2, "event_key": item.EventKey, "status": "answered", "analysis_status": "fixed", "interpretation_mode": "fixed", "template_version": "alarm-fixed/v1", "model": "", "conclusion": gin.H{"text": text, "evidence_ids": []string{"E1"}}, "findings": []any{}, "hypotheses": []any{}, "suggestions": suggestions, "limitations": []string{"固定规则解读，未调用模型API；内容为通用核查提示，不是AI生成的故障诊断。", "平台当前使用合成数据，演示异常不能作为真实场站故障依据。"}, "facts": eventFacts(evidence), "evidence": evidence, "reason": "", "retryable": false}
}
