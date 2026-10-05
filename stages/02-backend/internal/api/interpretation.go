package api

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/redis/go-redis/v9"
	"gorm.io/gorm"
	"powersystem/backend/internal/model"
	"powersystem/backend/internal/telemetry"
)

const monitorStream = "stage7:monitor:events"
const monitorGroup = "stage7-go"

type interpretationTask struct {
	ID         int64
	EventKey   string
	Category   string
	AlarmID    *int64
	Level      string
	OccurredAt time.Time
	Attempts   int
	LeaseToken string
	Evidence   json.RawMessage
}

type eventEvidence struct {
	ID          string  `json:"id"`
	Tool        string  `json:"tool"`
	Status      string  `json:"status"`
	Source      string  `json:"source"`
	CollectedAt string  `json:"collected_at"`
	DataTime    *string `json:"data_time"`
	Data        any     `json:"data"`
}

func (s *Server) interpretationScanner(ctx context.Context) {
	tick := time.NewTicker(time.Second)
	defer tick.Stop()
	for {
		if err := s.scanInterpretations(ctx); err != nil && ctx.Err() == nil {
			s.log.Warn("interpretation_scan_failed")
		}
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
		}
	}
}

func (s *Server) scanInterpretations(ctx context.Context) error {
	return s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		if err := tx.Exec(`INSERT INTO agent_scan_cursor(name,baseline_id,last_id)
   SELECT 'alarms',COALESCE(MAX(id),0),COALESCE(MAX(id),0) FROM alarm_records
   ON CONFLICT(name) DO NOTHING`).Error; err != nil {
			return err
		}
		var cursor struct {
			BaselineID int64
			LastID     int64
		}
		if err := tx.Raw("SELECT baseline_id,last_id FROM agent_scan_cursor WHERE name='alarms' FOR UPDATE").Scan(&cursor).Error; err != nil {
			return err
		}
		var alarms []model.AlarmRecord
		// Anti-join above the initial baseline also catches lower IDs whose original
		// transactions commit after a higher ID. last_id is progress, not exclusion.
		if err := tx.Raw(`SELECT a.* FROM alarm_records a LEFT JOIN agent_interpretations i ON i.alarm_id=a.id
   WHERE a.id>? AND i.id IS NULL ORDER BY a.id LIMIT 100`, cursor.BaselineID).Scan(&alarms).Error; err != nil {
			return err
		}
		for _, a := range alarms {
			category := "device_alarm"
			if a.Metric == aiMetric {
				category = "prediction_risk"
			}
			if err := tx.Exec(`INSERT INTO agent_interpretations(event_key,category,alarm_id,level,occurred_at)
    VALUES(?,?,?,?,?) ON CONFLICT(event_key) DO NOTHING`, fmt.Sprintf("alarm:%d", a.ID), category, a.ID, a.Level, a.TriggeredAt).Error; err != nil {
				return err
			}
			if a.ID > cursor.LastID {
				cursor.LastID = a.ID
			}
		}
		return tx.Exec("UPDATE agent_scan_cursor SET last_id=? WHERE name='alarms'", cursor.LastID).Error
	})
}

func (s *Server) monitorConsumer(ctx context.Context) {
	consumer, _ := randomTicket()
	for ctx.Err() == nil {
		err := s.redis.XGroupCreateMkStream(ctx, monitorStream, monitorGroup, "0").Err()
		if err != nil && !strings.Contains(err.Error(), "BUSYGROUP") {
			select {
			case <-ctx.Done():
				return
			case <-time.After(time.Second):
			}
			continue
		}
		reclaimed, _, err := s.redis.XAutoClaim(ctx, &redis.XAutoClaimArgs{Stream: monitorStream, Group: monitorGroup, Consumer: consumer, MinIdle: 5 * time.Second, Start: "0-0", Count: 10}).Result()
		if err == nil {
			for _, msg := range reclaimed {
				s.consumeMonitor(ctx, msg)
			}
		}
		batches, err := s.redis.XReadGroup(ctx, &redis.XReadGroupArgs{Group: monitorGroup, Consumer: consumer, Streams: []string{monitorStream, ">"}, Count: 10, Block: time.Second}).Result()
		if err != nil && !errors.Is(err, redis.Nil) {
			s.log.Warn("monitor_consumer_unavailable")
			select {
			case <-ctx.Done():
				return
			case <-time.After(time.Second):
			}
			continue
		}
		for _, batch := range batches {
			for _, msg := range batch.Messages {
				s.consumeMonitor(ctx, msg)
			}
		}
	}
}

func (s *Server) consumeMonitor(ctx context.Context, msg redis.XMessage) {
	raw, ok := msg.Values["payload"].(string)
	if !ok {
		return
	}
	var batch struct {
		Alerts []struct {
			Status      string            `json:"status"`
			Fingerprint string            `json:"fingerprint"`
			StartsAt    time.Time         `json:"startsAt"`
			EndsAt      time.Time         `json:"endsAt"`
			Labels      map[string]string `json:"labels"`
			Annotations map[string]string `json:"annotations"`
		} `json:"alerts"`
	}
	if json.Unmarshal([]byte(raw), &batch) != nil {
		s.log.Warn("monitor_payload_invalid")
		return
	}
	err := s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		for _, a := range batch.Alerts {
			if a.Labels["alertname"] != "PowerSystemAPIDown" && a.Labels["alertname"] != "PowerSystemDiskHigh" {
				continue
			}
			key := "monitor:" + a.Fingerprint + ":" + a.StartsAt.UTC().Format(time.RFC3339Nano)
			level := "major"
			if a.Labels["severity"] == "critical" {
				level = "urgent"
			}
			var recovered any
			if a.Status == "resolved" {
				recovered = a.EndsAt
			}
			payload, _ := json.Marshal(a)
			if err := tx.Exec(`INSERT INTO agent_monitor_events(event_key,level,occurred_at,recovered_at,payload)
    VALUES(?,?,?,?,?::jsonb) ON CONFLICT(event_key) DO UPDATE SET
    recovered_at=COALESCE(agent_monitor_events.recovered_at,EXCLUDED.recovered_at),updated_at=now()`, key, level, a.StartsAt, recovered, string(payload)).Error; err != nil {
				return err
			}
			if err := tx.Exec(`INSERT INTO agent_interpretations(event_key,category,level,occurred_at)
    VALUES(?,'platform_monitor',?,?) ON CONFLICT(event_key) DO NOTHING`, key, level, a.StartsAt).Error; err != nil {
				return err
			}
			if err := tx.Exec("UPDATE agent_interpretations SET updated_at=now() WHERE event_key=?", key).Error; err != nil {
				return err
			}
		}
		return nil
	})
	if err != nil {
		s.log.Warn("monitor_persist_failed")
		return
	}
	// A crash between commit and ACK is safe: PostgreSQL event keys deduplicate.
	if s.redis.XAck(ctx, monitorStream, monitorGroup, msg.ID).Err() == nil {
		_ = s.redis.XDel(ctx, monitorStream, msg.ID).Err()
	}
	s.broadcast(gin.H{"type": "interpretation_updated", "data": gin.H{"updated_at": time.Now().UTC()}})
}

func (s *Server) interpretationWorker(ctx context.Context) {
	for ctx.Err() == nil {
		token, _ := randomTicket()
		var item interpretationTask
		err := s.db.WithContext(ctx).Raw(`UPDATE agent_interpretations SET task_status='running',
   lease_until=now()+interval '60 seconds',lease_token=?,attempts=attempts+1,updated_at=now()
   WHERE id=(SELECT id FROM agent_interpretations WHERE
    (task_status='pending' AND next_attempt_at<=now()) OR (task_status='running' AND lease_until<now())
    ORDER BY occurred_at,id FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING *`, token).Scan(&item).Error
		if err != nil {
			s.log.Warn("interpretation_claim_failed")
		}
		if err == nil && item.ID != 0 {
			s.processInterpretation(ctx, item)
			continue
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(time.Second):
		}
	}
}

func (s *Server) buildEventEvidence(ctx context.Context, item interpretationTask) ([]eventEvidence, error) {
	stamp := time.Now().UTC().Format(time.RFC3339Nano)
	eventTime := item.OccurredAt.UTC().Format(time.RFC3339Nano)
	evidence := []eventEvidence{}
	add := func(tool, status, source string, data any, dataTime *string) {
		evidence = append(evidence, eventEvidence{fmt.Sprintf("E%d", len(evidence)+1), tool, status, source, stamp, dataTime, data})
	}
	if item.AlarmID == nil {
		var row struct{ Payload json.RawMessage }
		if err := s.db.WithContext(ctx).Raw("SELECT payload FROM agent_monitor_events WHERE event_key=?", item.EventKey).Scan(&row).Error; err != nil {
			return nil, err
		}
		if len(row.Payload) == 0 {
			return nil, errors.New("missing monitor")
		}
		var data map[string]any
		if json.Unmarshal(row.Payload, &data) != nil {
			return nil, errors.New("invalid monitor")
		}
		add("monitor_event", "ok", "alertmanager/webhook", data, &eventTime)
		return evidence, nil
	}
	var alarm model.AlarmRecord
	if err := s.db.WithContext(ctx).First(&alarm, *item.AlarmID).Error; err != nil {
		return nil, err
	}
	// Immutable trigger fields: do not present later acknowledgement as a trigger fact.
	add("get_alarm_detail", "ok", fmt.Sprintf("/api/v1/alarms/%d", alarm.ID), gin.H{"id": alarm.ID, "device_id": alarm.DeviceID, "metric": alarm.Metric, "level": alarm.Level, "value": alarm.Value, "threshold": alarm.Threshold, "triggered_at": alarm.TriggeredAt}, &eventTime)
	var device model.Device
	if err := s.db.WithContext(ctx).First(&device, alarm.DeviceID).Error; err != nil {
		add("device", "no_data", "devices", gin.H{}, nil)
	} else {
		add("device", "ok", fmt.Sprintf("/api/v1/devices/%d", device.ID), device, &stamp)
	}
	var history []model.AlarmRecord
	if err := s.db.WithContext(ctx).Where("device_id=? AND triggered_at>=? AND triggered_at<?", alarm.DeviceID, alarm.TriggeredAt.Add(-24*time.Hour), alarm.TriggeredAt).Order("triggered_at DESC,id DESC").Limit(21).Find(&history).Error; err != nil {
		add("list_alarms", "backend_unavailable", "alarm_records", gin.H{}, nil)
	} else {
		complete := len(history) <= 20
		if len(history) > 20 {
			history = history[:20]
		}
		// Only trigger facts are used: recovery/ack timestamps may be after this event.
		facts := []gin.H{}
		for _, a := range history {
			facts = append(facts, gin.H{"id": a.ID, "metric": a.Metric, "value": a.Value, "threshold": a.Threshold, "triggered_at": a.TriggeredAt})
		}
		add("list_alarms", "ok", "alarm_records:preceding_24h", gin.H{"list": facts, "complete": complete}, &eventTime)
	}
	metric := alarm.Metric
	if metric == aiMetric {
		metric = "temperature"
	}
	if telemetry.Metrics[metric] {
		toolCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
		rows, err := s.td.History(toolCtx, alarm.DeviceID, metric, alarm.TriggeredAt.Add(-30*time.Minute).UnixMilli(), alarm.TriggeredAt.UnixMilli())
		cancel()
		if err != nil {
			add("get_telemetry", "backend_unavailable", "tdengine/history", gin.H{}, nil)
		} else {
			points := [][]any{}
			var sum, min, max float64
			valid := 0
			for _, row := range rows {
				if len(row) != 2 {
					continue
				}
				value, err := strconv.ParseFloat(fmt.Sprint(row[1]), 64)
				if err != nil {
					continue
				}
				if valid == 0 || value < min {
					min = value
				}
				if valid == 0 || value > max {
					max = value
				}
				sum += value
				valid++
			}
			if len(rows) <= 100 {
				points = rows
			} else {
				for i := 0; i < 100; i++ {
					points = append(points, rows[i*(len(rows)-1)/99])
				}
			}
			status := "ok"
			var mean any
			if valid == 0 {
				status = "no_data"
			} else {
				mean = sum / float64(valid)
			}
			add("get_telemetry", status, fmt.Sprintf("/api/v1/devices/%d/telemetry?metric=%s&start=%s&end=%s", alarm.DeviceID, metric, alarm.TriggeredAt.Add(-30*time.Minute).UTC().Format(time.RFC3339), eventTime), gin.H{"device_id": alarm.DeviceID, "metric": metric, "points": points, "original_points": len(rows), "compressed": len(rows) > 100, "statistics": gin.H{"count": valid, "min": min, "max": max, "mean": mean}}, &eventTime)
		}
	}
	if alarm.Metric == aiMetric {
		var prediction model.PredictionRecord
		// Timestamp and trigger values must match; no use of a later/latest prediction.
		err := s.db.WithContext(ctx).Where("device_id=? AND created_at<=? AND window_end_ms<=? AND window_end_ms>=? AND probability=? AND threshold=?", alarm.DeviceID, alarm.TriggeredAt, alarm.TriggeredAt.UnixMilli(), alarm.TriggeredAt.Add(-15*time.Minute).UnixMilli(), alarm.Value, alarm.Threshold).Order("created_at DESC,id DESC").First(&prediction).Error
		if err != nil {
			add("get_prediction", "no_data", "prediction_records:trigger", gin.H{}, nil)
		} else {
			dataTime := time.UnixMilli(prediction.WindowEndMS).UTC().Format(time.RFC3339Nano)
			add("get_prediction", "ok", "prediction_records:trigger", prediction, &dataTime)
		}
	}
	return evidence, nil
}

func (s *Server) callAgent(ctx context.Context, method, path string, body any) (map[string]any, error) {
	if !s.cfg.AgentEnabled || s.cfg.AgentToken == "" {
		return nil, errors.New("not_configured")
	}
	raw, _ := json.Marshal(body)
	callCtx, cancel := context.WithTimeout(ctx, 45*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(callCtx, method, s.cfg.AgentURL+path, bytes.NewReader(raw))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-PowerSystem-Token", s.cfg.AgentToken)
	resp, err := (&http.Client{Timeout: 45 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}).Do(req)
	if err != nil {
		return nil, errors.New("unavailable")
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return nil, errors.New("unavailable")
	}
	payload, err := io.ReadAll(io.LimitReader(resp.Body, (256<<10)+1))
	if err != nil || len(payload) > 256<<10 {
		return nil, errors.New("unavailable")
	}
	var result map[string]any
	if json.Unmarshal(payload, &result) != nil {
		return nil, errors.New("unavailable")
	}
	return result, nil
}

func retryInterpretation(reason string, attempts int) bool {
	return attempts < 3 && reason != "not_configured" && reason != "auth_failed" && reason != "quota_exhausted" && reason != "answer_validation_failed" && reason != "missing_event_evidence"
}

func (s *Server) processInterpretation(ctx context.Context, item interpretationTask) {
	var evidence []eventEvidence
	reason := ""
	if len(item.Evidence) == 0 {
		var err error
		evidence, err = s.buildEventEvidence(ctx, item)
		if err != nil {
			reason = "unavailable"
		} else {
			raw, _ := json.Marshal(evidence)
			if len(raw) > 65536 {
				reason = "evidence_budget_exceeded"
			} else {
				updated := s.db.WithContext(ctx).Exec("UPDATE agent_interpretations SET evidence=?::jsonb WHERE id=? AND lease_token=?", string(raw), item.ID, item.LeaseToken)
				if updated.Error != nil || updated.RowsAffected != 1 {
					return
				}
			}
		}
	} else if json.Unmarshal(item.Evidence, &evidence) != nil {
		reason = "missing_event_evidence"
	}
	result := map[string]any{"status": "degraded", "conclusion": nil, "suggestions": []any{}, "evidence": evidence, "limitations": []string{"智能解读暂不可用，原因尚无法判断。"}, "reason": reason}
	if reason == "" {
		response, err := s.callAgent(ctx, http.MethodPost, "/internal/agent/interpret", gin.H{"event_key": item.EventKey, "category": item.Category, "evidence": evidence})
		if err != nil {
			reason = err.Error()
		} else {
			status, _ := response["status"].(string)
			if response["event_key"] != item.EventKey || (status != "answered" && status != "degraded" && status != "unable_to_determine") {
				reason = "unavailable"
			} else {
				result = response
				reason, _ = response["reason"].(string)
			}
		}
	}
	result["reason"] = reason
	taskStatus := "completed"
	if result["status"] != "answered" {
		taskStatus = "degraded"
	}
	delay := 5 * time.Second
	if item.Attempts >= 2 {
		delay = 30 * time.Second
	}
	if value, ok := result["retry_after"].(float64); ok && value > delay.Seconds() {
		if value > 60 {
			value = 60
		}
		delay = time.Duration(value) * time.Second
	}
	if reason != "" && retryInterpretation(reason, item.Attempts) {
		taskStatus = "pending"
	}
	// Evidence stored and served by Go is authoritative, not vendor output.
	result["evidence"] = evidence
	raw, _ := json.Marshal(result)
	updated := s.db.WithContext(ctx).Exec(`UPDATE agent_interpretations SET task_status=?,result=?::jsonb,reason=?,next_attempt_at=?,
  lease_until=NULL,lease_token=NULL,updated_at=now() WHERE id=? AND lease_token=?`, taskStatus, string(raw), reason, time.Now().Add(delay), item.ID, item.LeaseToken)
	if updated.Error == nil && updated.RowsAffected == 1 {
		s.broadcast(gin.H{"type": "interpretation_updated", "data": gin.H{"id": item.ID, "status": taskStatus, "updated_at": time.Now().UTC()}})
		if reason == "auth_failed" || reason == "quota_exhausted" || reason == "not_configured" {
			s.broadcast(gin.H{"type": "agent_model_updated", "data": gin.H{"available": false}})
		}
	}
}

const interpretationSelect = `SELECT jsonb_build_object('id',i.id,'event_key',i.event_key,'category',i.category,
 'alarm_id',i.alarm_id,'level',i.level,'occurred_at',i.occurred_at,'task_status',i.task_status,
 'reason',i.reason,'result',i.result,'evidence',i.evidence,'updated_at',i.updated_at,
 'read',r.user_id IS NOT NULL,'alarm',to_jsonb(a),'monitor',m.payload || jsonb_build_object('recovered_at',m.recovered_at),
 'active',CASE WHEN i.alarm_id IS NOT NULL THEN a.recovered_at IS NULL ELSE m.recovered_at IS NULL END) AS payload
 FROM agent_interpretations i LEFT JOIN alarm_records a ON a.id=i.alarm_id
 LEFT JOIN agent_monitor_events m ON m.event_key=i.event_key
 LEFT JOIN agent_interpretation_reads r ON r.interpretation_id=i.id AND r.user_id=? `

func (s *Server) listInterpretations(c *gin.Context) {
	p, size, valid := page(c)
	if !valid {
		return
	}
	where := " WHERE true"
	args := []any{c.GetInt64("user_id")}
	if category := c.Query("category"); category != "" {
		if category != "device_alarm" && category != "prediction_risk" && category != "platform_monitor" {
			fail(c, 400, 40001, "invalid category")
			return
		}
		where += " AND i.category=?"
		args = append(args, category)
	}
	if raw := c.Query("alarm_id"); raw != "" {
		id, err := strconv.ParseInt(raw, 10, 64)
		if err != nil || id < 1 {
			fail(c, 400, 40001, "invalid alarm_id")
			return
		}
		where += " AND i.alarm_id=?"
		args = append(args, id)
	}
	if raw := c.Query("updated_after"); raw != "" {
		stamp, err := time.Parse(time.RFC3339Nano, raw)
		if err != nil {
			fail(c, 400, 40001, "invalid updated_after")
			return
		}
		where += " AND i.updated_at>=?"
		args = append(args, stamp)
	}
	if c.Query("attention") == "true" {
		where += ` AND r.user_id IS NULL AND (i.occurred_at>now()-interval '24 hours' OR
  (i.alarm_id IS NOT NULL AND a.recovered_at IS NULL) OR (i.alarm_id IS NULL AND m.recovered_at IS NULL))`
	}
	var total int64
	countSQL := `SELECT count(*) FROM (` + interpretationSelect + where + `) q`
	if s.db.WithContext(c.Request.Context()).Raw(countSQL, args...).Scan(&total).Error != nil {
		fail(c, 503, 50301, "interpretations unavailable")
		return
	}
	var rows []struct{ Payload json.RawMessage }
	args = append(args, size, (p-1)*size)
	if s.db.WithContext(c.Request.Context()).Raw(interpretationSelect+where+" ORDER BY CASE i.level WHEN 'urgent' THEN 0 WHEN 'major' THEN 1 ELSE 2 END,i.occurred_at DESC,i.id DESC LIMIT ? OFFSET ?", args...).Scan(&rows).Error != nil {
		fail(c, 503, 50301, "interpretations unavailable")
		return
	}
	data := []json.RawMessage{}
	for _, row := range rows {
		data = append(data, row.Payload)
	}
	ok(c, list(data, p, size, total))
}

func (s *Server) getInterpretation(c *gin.Context) {
	itemID, valid := id(c)
	if !valid {
		return
	}
	var row struct{ Payload json.RawMessage }
	if s.db.WithContext(c.Request.Context()).Raw(interpretationSelect+" WHERE i.id=?", c.GetInt64("user_id"), itemID).Scan(&row).Error != nil {
		fail(c, 503, 50301, "interpretation unavailable")
		return
	}
	if len(row.Payload) == 0 {
		fail(c, 404, 40401, "interpretation not found")
		return
	}
	ok(c, row.Payload)
}

func (s *Server) readInterpretation(c *gin.Context) {
	itemID, valid := id(c)
	if !valid {
		return
	}
	result := s.db.WithContext(c.Request.Context()).Exec(`INSERT INTO agent_interpretation_reads(interpretation_id,user_id)
  SELECT id,? FROM agent_interpretations WHERE id=? ON CONFLICT(interpretation_id,user_id) DO UPDATE SET read_at=now()`, c.GetInt64("user_id"), itemID)
	if result.Error != nil {
		fail(c, 503, 50301, "read status unavailable")
		return
	}
	if result.RowsAffected == 0 {
		fail(c, 404, 40401, "interpretation not found")
		return
	}
	ok(c, gin.H{"id": itemID, "read": true})
}

func (s *Server) agentModelStatus(c *gin.Context) {
	result, err := s.callAgent(c.Request.Context(), http.MethodGet, "/internal/agent/model-status", nil)
	if err != nil {
		ok(c, gin.H{"available": false, "reason": "unavailable", "message": "智能体不可用或未启用，请检查服务器私有配置。"})
		return
	}
	ok(c, result)
}

func (s *Server) agentModelProbe(c *gin.Context) {
	allowed, err := s.redis.SetNX(c.Request.Context(), "stage7:model:probe-limit", "1", time.Minute).Result()
	if err != nil {
		fail(c, 503, 50301, "probe limiter unavailable")
		return
	}
	if !allowed {
		fail(c, 429, 42901, "model probe limited to once per minute")
		return
	}
	result, err := s.callAgent(c.Request.Context(), http.MethodPost, "/internal/agent/probe", nil)
	if err != nil {
		fail(c, 503, 50301, "model probe failed; inspect model status")
		return
	}
	if result["chat"] != true || result["tools"] != true {
		fail(c, 503, 50301, "invalid model probe")
		return
	}
	if err := s.db.WithContext(c.Request.Context()).Exec(`UPDATE agent_interpretations i SET task_status='pending',attempts=0,next_attempt_at=now(),updated_at=now()
  WHERE task_status='degraded' AND reason IN ('not_configured','auth_failed','quota_exhausted') AND occurred_at>now()-interval '24 hours'
  AND ((alarm_id IS NOT NULL AND EXISTS(SELECT 1 FROM alarm_records a WHERE a.id=i.alarm_id AND a.recovered_at IS NULL))
  OR (alarm_id IS NULL AND EXISTS(SELECT 1 FROM agent_monitor_events m WHERE m.event_key=i.event_key AND m.recovered_at IS NULL)))`).Error; err != nil {
		fail(c, 503, 50301, "model ready but replay unavailable")
		return
	}
	s.broadcast(gin.H{"type": "agent_model_updated", "data": gin.H{"available": true}})
	ok(c, result)
}
