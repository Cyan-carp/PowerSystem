package api

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"math/rand/v2"
	"net/http"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/redis/go-redis/v9"
)

type interpretationFact struct {
	ID         string   `json:"fact_id"`
	EvidenceID string   `json:"evidence_id"`
	Path       string   `json:"path"`
	Label      string   `json:"label"`
	Role       string   `json:"role"`
	Type       string   `json:"value_type"`
	Value      any      `json:"value"`
	Display    string   `json:"display_value"`
	Unit       string   `json:"unit"`
	Precision  int      `json:"precision"`
	Timezone   string   `json:"timezone"`
	Formula    string   `json:"formula,omitempty"`
	Inputs     []string `json:"input_fact_ids,omitempty"`
}

var factLabels = map[string]string{"value": "触发值", "threshold": "告警阈值", "probability": "预测概率",
	"triggered_at": "触发时间", "created_at": "记录时间", "window_end_ms": "预测窗口结束时间",
	"device_code": "设备编码", "station_code": "场站编码", "name": "设备名称", "metric": "指标",
	"min": "最低值", "max": "最高值", "mean": "平均值", "count": "样本数", "complete": "查询完整",
	"model_version": "预测模型版本", "risk_level": "预测风险等级", "level": "告警级别", "original_points": "原始点数",
	"status": "事件状态", "startsAt": "监控开始时间", "alertname": "监控名称", "severity": "监控级别",
	"scenario_id": "演示情景编号", "scenario_label": "演示说明", "origin": "事件来源"}

func eventFacts(evidence []eventEvidence) []interpretationFact {
	facts := []interpretationFact{}
	add := func(e eventEvidence, path, key string, value any, unit string) {
		if len(facts) >= 200 {
			return
		}
		label, ok := factLabels[key]
		if !ok {
			return
		}
		f := interpretationFact{ID: fmt.Sprintf("F%d", len(facts)+1), EvidenceID: e.ID, Path: path, Label: label, Role: key, Value: value, Precision: 3, Unit: unit}
		switch v := value.(type) {
		case float64:
			if math.IsNaN(v) || math.IsInf(v, 0) {
				return
			}
			f.Type = "number"
			f.Display = strconv.FormatFloat(v, 'f', 3, 64)
			f.Display = strings.TrimRight(strings.TrimRight(f.Display, "0"), ".")
			if key == "window_end_ms" {
				t := time.UnixMilli(int64(v))
				f.Type = "time"
				f.Value = t.UTC().Format(time.RFC3339Nano)
				f.Display = t.In(time.FixedZone("Asia/Shanghai", 8*3600)).Format("2006-01-02 15:04:05 -07:00")
				f.Timezone = "Asia/Shanghai"
				f.Unit = ""
			}
		case string:
			if len(v) > 300 || strings.TrimSpace(v) == "" {
				return
			}
			f.Type = "text"
			f.Display = v
			if t, err := time.Parse(time.RFC3339Nano, v); err == nil {
				f.Type = "time"
				f.Value = t.UTC().Format(time.RFC3339Nano)
				f.Display = t.In(time.FixedZone("Asia/Shanghai", 8*3600)).Format("2006-01-02 15:04:05 -07:00")
				f.Timezone = "Asia/Shanghai"
			}
		case bool:
			f.Type = "boolean"
			f.Display = "否"
			if v {
				f.Display = "是"
			}
		default:
			return
		}
		if key == "value" {
			f.Role = "alarm_value"
		}
		if key == "threshold" {
			f.Role = "alarm_threshold"
		}
		if e.Tool == "get_prediction" {
			if key == "threshold" {
				f.Role = "prediction_threshold"
				f.Label = "预测阈值"
			}
			if key == "probability" {
				f.Role = "prediction_probability"
			}
		}
		if e.Tool == "get_alarm_detail" {
			if m, ok := e.Data.(map[string]any); ok && m["metric"] == "ai_failure_risk" && key == "value" {
				f.Role = "alarm_probability"
				f.Label = "触发预测概率"
			}
		}
		facts = append(facts, f)
	}
	for _, e := range evidence {
		if e.Status != "ok" {
			continue
		}
		data, ok := e.Data.(map[string]any)
		if !ok {
			continue
		}
		metric, _ := data["metric"].(string)
		unit := map[string]string{"temperature": "℃", "power": "kW", "voltage": "V", "current": "A"}[metric]
		var walk func(map[string]any, string)
		walk = func(m map[string]any, prefix string) {
			keys := make([]string, 0, len(m))
			for k := range m {
				keys = append(keys, k)
			}
			sort.Strings(keys)
			for _, k := range keys {
				v := m[k]
				path := prefix + k
				if child, ok := v.(map[string]any); ok {
					if k == "statistics" || k == "labels" {
						walk(child, path+".")
					}
					continue
				}
				fieldUnit := ""
				if k == "value" || k == "threshold" || k == "min" || k == "max" || k == "mean" {
					fieldUnit = unit
				}
				add(e, path, k, v, fieldUnit)
			}
		}
		walk(data, "")
		if e.Tool == "list_alarms" {
			if list, ok := data["list"].([]any); ok {
				add(e, "derived.returned_count", "count", float64(len(list)), "")
			}
		}
	}
	// Only explicitly defined, provenance-carrying arithmetic is permitted.
	var value, threshold *interpretationFact
	for i := range facts {
		f := &facts[i]
		if f.EvidenceID == "E1" && f.Role == "alarm_value" && f.Type == "number" {
			value = f
		}
		if f.EvidenceID == "E1" && f.Role == "alarm_threshold" && f.Type == "number" {
			threshold = f
		}
	}
	if value != nil && threshold != nil && value.Unit == threshold.Unit && len(facts) < 200 {
		v := value.Value.(float64) - threshold.Value.(float64)
		facts = append(facts, interpretationFact{ID: fmt.Sprintf("F%d", len(facts)+1), EvidenceID: value.EvidenceID, Path: "derived.value_minus_threshold", Label: "触发值与阈值差", Role: "alarm_delta", Type: "number", Value: v, Display: strconv.FormatFloat(v, 'f', 3, 64), Unit: value.Unit, Precision: 3, Formula: "subtract", Inputs: []string{value.ID, threshold.ID}})
	}
	return facts
}

type factTemplate struct {
	Template string   `json:"template"`
	Refs     []string `json:"fact_refs"`
}
type factAnalysis struct {
	Findings    []factTemplate `json:"findings"`
	Hypotheses  []factTemplate `json:"hypotheses"`
	Suggestions []factTemplate `json:"suggestions"`
	Limitations []factTemplate `json:"limitations"`
}

var factToken = regexp.MustCompile(`\{\{(F\d{1,4})\}\}`)
var rawClaimNumber = regexp.MustCompile(`[0-9]|[零〇一二两三四五六七八九十百千万]+\s*(倍|分钟|小时|秒|天|度|伏|安|千瓦|百分)`)

func renderFactClaim(claim factTemplate, facts []interpretationFact) (gin.H, error) {
	byID := map[string]interpretationFact{}
	for _, f := range facts {
		byID[f.ID] = f
	}
	if len(claim.Template) == 0 || len(claim.Template) > 3600 || len(claim.Refs) == 0 || len(claim.Refs) > 10 {
		return nil, errors.New("answer_schema_invalid")
	}
	refs := map[string]bool{}
	evidenceIDs := []string{}
	evidenceSeen := map[string]bool{}
	for _, ref := range claim.Refs {
		f, ok := byID[ref]
		if !ok || refs[ref] {
			return nil, errors.New("invalid_fact_reference")
		}
		refs[ref] = true
		if !evidenceSeen[f.EvidenceID] {
			evidenceIDs = append(evidenceIDs, f.EvidenceID)
			evidenceSeen[f.EvidenceID] = true
		}
	}
	invalid := false
	residual := factToken.ReplaceAllString(claim.Template, "")
	if strings.Contains(residual, "{{") || strings.Contains(residual, "}}") || rawClaimNumber.MatchString(residual) {
		return nil, errors.New("unsupported_numeric_claim")
	}
	rendered := factToken.ReplaceAllStringFunc(claim.Template, func(token string) string {
		ref := factToken.FindStringSubmatch(token)[1]
		if !refs[ref] {
			invalid = true
			return ""
		}
		f := byID[ref]
		return f.Label + "：" + f.Display + f.Unit
	})
	if invalid {
		return nil, errors.New("invalid_fact_reference")
	}
	return gin.H{"text": rendered, "evidence_ids": evidenceIDs}, nil
}

func renderFactAnalysis(response map[string]any, facts []interpretationFact) error {
	raw, _ := json.Marshal(response["analysis"])
	var a factAnalysis
	decoder := json.NewDecoder(strings.NewReader(string(raw)))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&a) != nil || len(a.Findings) < 1 || len(a.Findings) > 5 || len(a.Hypotheses) > 3 || len(a.Suggestions) > 5 || len(a.Limitations) > 8 {
		return errors.New("answer_schema_invalid")
	}
	render := func(claims []factTemplate) ([]gin.H, error) {
		out := []gin.H{}
		for _, c := range claims {
			v, err := renderFactClaim(c, facts)
			if err != nil {
				return nil, err
			}
			out = append(out, v)
		}
		return out, nil
	}
	findings, err := render(a.Findings)
	if err != nil {
		return err
	}
	hypotheses, err := render(a.Hypotheses)
	if err != nil {
		return err
	}
	suggestions, err := render(a.Suggestions)
	if err != nil {
		return err
	}
	limitations, err := render(a.Limitations)
	if err != nil {
		return err
	}
	conclusion := gin.H{"text": "", "evidence_ids": []string{}}
	text := []string{}
	refs := []string{}
	seen := map[string]bool{}
	for _, f := range findings {
		text = append(text, f["text"].(string))
		for _, id := range f["evidence_ids"].([]string) {
			if !seen[id] {
				refs = append(refs, id)
				seen[id] = true
			}
		}
	}
	conclusion["text"] = strings.Join(text, "\n")
	conclusion["evidence_ids"] = refs
	notes := []string{"模型分析仅供人工核查；事件事实为历史快照，不证明设备根因。"}
	for _, l := range limitations {
		notes = append(notes, l["text"].(string))
	}
	response["conclusion"] = conclusion
	for _, claim := range suggestions {
		claim["equipment_operation"] = true
	}
	for _, claim := range hypotheses {
		claim["text"] = "可能原因（未证实）：" + claim["text"].(string)
	}
	response["suggestions"] = suggestions
	response["limitations"] = notes
	response["findings"] = findings
	response["hypotheses"] = hypotheses
	return nil
}

const circuitKey = "stage7:interpret:circuit:deepseek-flash"

var circuitPermit = redis.NewScript(`
local now=tonumber(ARGV[1]);local untilAt=tonumber(redis.call('HGET',KEYS[1],'until') or '0');local failures=tonumber(redis.call('HGET',KEYS[1],'failures') or '0');local gen=tonumber(redis.call('HGET',KEYS[1],'generation') or '0')
if untilAt>now then return {0,math.ceil((untilAt-now)/1000),gen,0} end
if failures>=3 then if not redis.call('SET',KEYS[2],ARGV[2],'NX','EX',50) then return {0,1,gen,0} end;return {1,0,gen,1} end
return {1,0,gen,0}`)
var circuitFinish = redis.NewScript(`
local gen=tonumber(redis.call('HGET',KEYS[1],'generation') or '0');if gen~=tonumber(ARGV[1]) then return 0 end
if ARGV[5]=='1' and redis.call('GET',KEYS[2])~=ARGV[6] then return 0 end
if ARGV[2]=='1' then redis.call('HSET',KEYS[1],'failures',0,'until',0,'cooldown',60,'generation',gen+1);redis.call('DEL',KEYS[2]);return 1 end
local n=redis.call('HINCRBY',KEYS[1],'failures',1);if n>=3 then local cool=tonumber(redis.call('HGET',KEYS[1],'cooldown') or '60');if ARGV[5]=='1' then cool=math.min(cool*2,300) end;redis.call('HSET',KEYS[1],'until',tonumber(ARGV[3])+cool*1000,'cooldown',cool,'generation',gen+1);redis.call('DEL',KEYS[2]) end
redis.call('EXPIRE',KEYS[1],86400);return n`)

func providerFailure(reason string) bool {
	switch reason {
	case "unavailable", "model_timeout", "model_unreachable", "rate_limited", "model_non_json", "model_invalid_envelope", "model_empty_choices", "model_invalid_role", "model_unexpected_tools", "model_empty_content", "model_invalid_finish", "model_unexpected_sse", "model_response_too_large", "thinking_mode_mismatch", "answer_schema_invalid":
		return true
	}
	return false
}
func retryV2(reason string, calls int) bool {
	return calls < 2 && (providerFailure(reason) || reason == "answer_truncated")
}
func (s *Server) interpretationVersion() int {
	if s.cfg.AgentInterpretProtocol == "v2" {
		return 2
	}
	return 1
}

func (s *Server) processInterpretationV2(ctx context.Context, item interpretationTask) {
	result := map[string]any{"protocol_version": 2, "event_key": item.EventKey, "status": "degraded", "analysis_status": "unavailable", "conclusion": nil, "suggestions": []any{}, "limitations": []string{"模型分析未完成；请核查事件事实。"}}
	reason := ""
	delay := 30 * time.Second
	terminal := true
	var evidence []eventEvidence
	raw := item.Evidence
	if len(raw) == 0 {
		var err error
		evidence, err = s.buildEventEvidence(ctx, item)
		if err != nil {
			reason = "evidence_unavailable"
		} else {
			payload, _ := json.Marshal(evidence)
			var stored struct{ Evidence json.RawMessage }
			err = s.db.WithContext(ctx).Raw("UPDATE agent_interpretations SET evidence=?::jsonb WHERE id=? AND lease_token=? RETURNING evidence", string(payload), item.ID, item.LeaseToken).Scan(&stored).Error
			if err != nil || len(stored.Evidence) == 0 {
				return
			}
			raw = stored.Evidence
		}
	}
	if reason == "" {
		if len(raw) > 65536 || json.Unmarshal(raw, &evidence) != nil {
			reason = "evidence_budget_exceeded"
		}
	}
	result["evidence"] = evidence
	facts := eventFacts(evidence)
	result["facts"] = facts
	if reason == "" {
		permitToken, _ := randomTicket()
		permit, err := circuitPermit.Run(ctx, s.redis, []string{circuitKey, circuitKey + ":half"}, time.Now().UnixMilli(), permitToken).Int64Slice()
		if err != nil || len(permit) != 4 {
			reason = "model_state_unavailable"
			terminal = false
			delay = time.Minute
		} else if permit[0] == 0 {
			reason = "circuit_open"
			terminal = false
			delay = time.Duration(permit[1]) * time.Second
		} else {
			var reservation struct{ ModelCalls int }
			reserved := s.db.WithContext(ctx).Raw(`UPDATE agent_interpretations SET model_calls=model_calls+1 WHERE id=? AND lease_token=? AND lease_until>now() AND model_calls<2 AND created_at>now()-interval '15 minutes' RETURNING model_calls`, item.ID, item.LeaseToken).Scan(&reservation)
			if reserved.Error != nil {
				reason = "model_state_unavailable"
			} else if reservation.ModelCalls == 0 {
				reason = "call_reservation_rejected"
			} else {
				digest := sha256.Sum256(raw)
				hash := fmt.Sprintf("%x", digest)
				origin := "normal"
				if item.AlarmID != nil {
					for _, e := range evidence {
						if m, ok := e.Data.(map[string]any); ok && m["metric"] == "simulation_scenario" {
							origin = "simulation_demo"
						}
					}
				}
				coverage := []string{}
				for _, e := range evidence {
					if e.Status != "ok" {
						coverage = append(coverage, e.ID+":"+e.Status)
					}
				}
				request := gin.H{"protocol_version": 2, "event_key": item.EventKey, "category": item.Category, "event_origin": origin, "evidence_version": 1, "evidence_hash": hash, "evidence": evidence, "facts": facts, "coverage": coverage}
				payload, _ := json.Marshal(request)
				if len(payload) > 65536 {
					reason = "evidence_budget_exceeded"
				} else {
					response, callErr := s.callAgent(ctx, http.MethodPost, "/internal/agent/interpret/v2", request)
					if callErr != nil {
						reason = callErr.Error()
					} else if response["protocol_version"] != float64(2) || response["event_key"] != item.EventKey || response["evidence_hash"] != hash {
						reason = "model_invalid_envelope"
					} else {
						status, _ := response["status"].(string)
						analysisStatus, _ := response["analysis_status"].(string)
						if status != "answered" && status != "degraded" && status != "unable_to_determine" {
							reason = "model_invalid_envelope"
						} else {
							result = response
							reason, _ = response["reason"].(string)
							if status == "answered" {
								if analysisStatus != "answered" {
									reason = "model_invalid_envelope"
								} else if err := renderFactAnalysis(result, facts); err != nil {
									reason = err.Error()
								}
							}
						}
					}
				}
				healthy := !providerFailure(reason)
				if reason == "model_state_unavailable" {
					healthy = false
				}
				if providerFailure(reason) || healthy {
					ok := 0
					if healthy {
						ok = 1
					}
					if _, err := circuitFinish.Run(ctx, s.redis, []string{circuitKey, circuitKey + ":half"}, permit[2], ok, time.Now().UnixMilli(), 0, permit[3], permitToken).Result(); err != nil {
						reason = "model_state_unavailable"
					}
				}
				if retryV2(reason, reservation.ModelCalls) {
					terminal = false
					delay = time.Duration(24000+rand.IntN(12001)) * time.Millisecond
					if after, ok := result["retry_after"].(float64); ok && after > delay.Seconds() {
						delay = time.Duration(after * float64(time.Second))
					}
				}
			}
		}
	}
	if reason != "" || result["status"] != "answered" {
		result["status"] = "degraded"
		if result["analysis_status"] == "answered" {
			result["analysis_status"] = "rejected"
		}
		result["conclusion"] = nil
		result["suggestions"] = []any{}
		result["hypotheses"] = []any{}
		result["analysis"] = nil
		result["limitations"] = []string{"模型分析未完成；请核查事件事实。"}
	}
	result["reason"] = reason
	result["error_code"] = reason
	result["retryable"] = !terminal
	result["facts"] = facts
	result["evidence"] = evidence
	state := "completed"
	if result["status"] != "answered" {
		state = "degraded"
	}
	if !terminal {
		state = "pending"
	}
	resultRaw, _ := json.Marshal(result)
	updated := s.db.WithContext(ctx).Exec(`UPDATE agent_interpretations SET task_status=?,result=?::jsonb,reason=?,next_attempt_at=?,lease_until=NULL,lease_token=NULL,updated_at=now() WHERE id=? AND lease_token=?`, state, string(resultRaw), reason, time.Now().Add(delay), item.ID, item.LeaseToken)
	if updated.Error == nil && updated.RowsAffected == 1 {
		s.broadcast(gin.H{"type": "interpretation_updated", "data": gin.H{"id": item.ID, "status": state, "updated_at": time.Now().UTC()}})
	}
}
