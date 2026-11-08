package api

// 预测质量的 AI 解读：把「为什么退化、该不该换」写成一段人话。
//
// 边界（见 V7 设计 04 篇）：
//   1. 只读：这里只查库、只调模型，不写 prediction_records / model_registry；
//   2. 不执行：模型输出的结论不会变成训练、试用、回退动作，只是文本；
//   3. 可降级：Key 缺失、超时、解析失败都返回 503，页面显示原始指标即可；
//   4. 可追溯：每次解读落库，带 prompt_version 与 token 用量。

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm"
)

const qualityAnalysisPromptVersion = "v1"

// qualityLLMThinking 控制是否开启深度思考。默认关闭，仍可显式开启做对比。
func (s *Server) qualityLLMThinking() string {
	if strings.EqualFold(strings.TrimSpace(s.cfg.QualityLLMThinking), "disabled") {
		return "disabled"
	}
	return "enabled"
}

func (s *Server) qualityLLMKey() string {
	if strings.TrimSpace(s.cfg.QualityLLMKeyFile) == "" {
		return ""
	}
	raw, err := os.ReadFile(s.cfg.QualityLLMKeyFile)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(raw))
}

type analysisRequest struct {
	// kind=drift：解释最近漂移；kind=trial：对比试用版本与上一版本
	Kind string `json:"kind"`
}

type analysisResult struct {
	ID               int64    `json:"id"`
	Kind             string   `json:"kind"`
	Subject          string   `json:"subject"`
	Conclusion       string   `json:"conclusion"`
	Reasons          []string `json:"reasons"`
	Suggestions      []string `json:"suggestions"`
	PromptTokens     int      `json:"prompt_tokens"`
	CompletionTokens int      `json:"completion_tokens"`
	CreatedAt        string   `json:"created_at"`
}

// qualityAnalysisFacts 汇总喂给模型的只读事实，字段名与前端表格一致，便于人工核对。
type qualityAnalysisFacts struct {
	Kind         string           `json:"kind"`
	Subject      string           `json:"subject"`
	ModelVersion string           `json:"model_version,omitempty"`
	EpochMS      int64            `json:"epoch_ms"`
	Hours        []hourStat       `json:"hours,omitempty"`
	Drift        []map[string]any `json:"drift,omitempty"`
	Trial        *trialFacts      `json:"trial,omitempty"`
	Baseline     map[string]any   `json:"baseline,omitempty"`
}

type hourStat struct {
	Hour      string `json:"hour"`
	TP        int64  `json:"tp"`
	FP        int64  `json:"fp"`
	FN        int64  `json:"fn"`
	Samples   int64  `json:"samples"`
	FaultRate string `json:"fault_rate"`
}

type trialFacts struct {
	Candidate     string `json:"candidate"`
	Previous      string `json:"previous"`
	CandidateNote string `json:"candidate_note"`
	PreviousNote  string `json:"previous_note"`
	StartedAt     string `json:"started_at"`
	ElapsedHours  string `json:"elapsed_hours"`
}

// collectAnalysisFacts 取事实。只统计 qualityEpochMS 之后、已成熟且按 1 小时规则标注的样本。
func (s *Server) collectAnalysisFacts(ctx context.Context, kind string) (qualityAnalysisFacts, error) {
	facts := qualityAnalysisFacts{Kind: kind, EpochMS: qualityEpochMS}
	closed := time.Now().Add(-time.Duration(predictionWindowMS) * time.Millisecond).UnixMilli()
	switch kind {
	case "drift":
		where := "window_end_ms >= ? AND window_end_ms <= ? AND label_rule = ?"
		args := []any{qualityEpochMS, closed, qualityLabelRule}
		// 与页面 KPI 一致：默认只解释区间内主导版本，避免多版本混算。
		var version struct {
			ModelVersion string `gorm:"column:model_version"`
		}
		if err := s.db.WithContext(ctx).Raw(`SELECT model_version FROM prediction_outcomes
			WHERE `+where+` GROUP BY model_version ORDER BY count(*) DESC, model_version ASC LIMIT 1`, args...).
			Scan(&version).Error; err != nil {
			return facts, err
		}
		if version.ModelVersion != "" {
			where += " AND model_version = ?"
			args = append(args, version.ModelVersion)
			facts.ModelVersion = version.ModelVersion
		}
		var rows []struct {
			Hour    string
			TP      int64
			FP      int64
			FN      int64
			Samples int64
		}
		err := s.db.WithContext(ctx).Raw(`
			SELECT to_char(to_timestamp(window_end_ms/1000) AT TIME ZONE 'Asia/Shanghai','HH24') AS hour,
			       sum(CASE WHEN predicted_high AND actual_fault THEN 1 ELSE 0 END) AS tp,
			       sum(CASE WHEN predicted_high AND NOT actual_fault THEN 1 ELSE 0 END) AS fp,
			       sum(CASE WHEN NOT predicted_high AND actual_fault THEN 1 ELSE 0 END) AS fn,
			       count(*) AS samples
			FROM prediction_outcomes
			WHERE `+where+`
			GROUP BY 1 ORDER BY 1`, args...).Scan(&rows).Error
		if err != nil {
			return facts, err
		}
		for _, row := range rows {
			faultRate := "0"
			if row.Samples > 0 {
				faultRate = fmt.Sprintf("%.2f%%", float64(row.TP+row.FN)*100/float64(row.Samples))
			}
			facts.Hours = append(facts.Hours, hourStat{
				Hour: row.Hour, TP: row.TP, FP: row.FP, FN: row.FN, Samples: row.Samples, FaultRate: faultRate,
			})
		}
		var events []map[string]any
		_ = s.db.WithContext(ctx).Raw(`
			SELECT level, signal, observed, baseline, threshold, action,
			       to_char(detected_at AT TIME ZONE 'Asia/Shanghai','MM-DD HH24:MI') AS at
			FROM prediction_drift_events ORDER BY detected_at DESC LIMIT 8`).Scan(&events).Error
		facts.Drift = events
		facts.Subject = "最近漂移与分时误差"
	case "trial":
		var trial struct {
			ModelVersion    string
			PreviousVersion *string
			TrialStartedAt  *time.Time
		}
		if err := s.db.WithContext(ctx).Raw(`
			SELECT model_version, previous_version, trial_started_at
			FROM model_registry WHERE status = 'trial' ORDER BY trial_started_at DESC LIMIT 1`).
			Scan(&trial).Error; err != nil {
			return facts, err
		}
		if trial.ModelVersion == "" {
			return facts, gorm.ErrRecordNotFound
		}
		previous := ""
		if trial.PreviousVersion != nil {
			previous = *trial.PreviousVersion
		}
		facts.Subject = trial.ModelVersion
		elapsed := "0"
		started := ""
		if trial.TrialStartedAt != nil {
			started = trial.TrialStartedAt.Format(time.RFC3339)
			elapsed = fmt.Sprintf("%.1f", time.Since(*trial.TrialStartedAt).Hours())
		}
		facts.Trial = &trialFacts{
			Candidate: trial.ModelVersion, Previous: previous,
			CandidateNote: "试用中，样本尚少", PreviousNote: "试用前的现役版本",
			StartedAt: started, ElapsedHours: elapsed,
		}
	default:
		return facts, errors.New("unsupported kind")
	}
	return facts, nil
}

// callQualityLLM 请求 DeepSeek。JSON 模式偶尔返回空白正文；仅对此重试一次，
// 第二次关闭 thinking 并退出 JSON 模式，避免再次得到空白。token 用量计入两次请求。
func (s *Server) callQualityLLM(ctx context.Context, system, user string) (string, int, int, error) {
	key := s.qualityLLMKey()
	if key == "" {
		return "", 0, 0, errors.New("llm key unavailable")
	}
	callCtx, cancel := context.WithTimeout(ctx, 90*time.Second)
	defer cancel()
	thinking := s.qualityLLMThinking()
	var promptTokens, completionTokens int
	for attempt := 0; attempt < 2; attempt++ {
		content, prompt, completion, err := s.callQualityLLMOnce(callCtx, key, thinking, attempt == 0, system, user)
		promptTokens += prompt
		completionTokens += completion
		if err != nil {
			return "", promptTokens, completionTokens, err
		}
		if strings.TrimSpace(content) != "" {
			return content, promptTokens, completionTokens, nil
		}
		thinking = "disabled"
	}
	return "", promptTokens, completionTokens, errors.New("模型未返回可用正文，请稍后重试")
}

func (s *Server) callQualityLLMOnce(ctx context.Context, key, thinking string, jsonMode bool, system, user string) (string, int, int, error) {
	body := map[string]any{
		"model": s.cfg.QualityLLMModel,
		"messages": []map[string]string{
			{"role": "system", "content": system},
			{"role": "user", "content": user},
		},
		"thinking":   map[string]string{"type": thinking},
		"stream":     false,
		"max_tokens": 1200,
	}
	if jsonMode {
		body["response_format"] = map[string]string{"type": "json_object"}
	}
	payload, _ := json.Marshal(body)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost,
		strings.TrimRight(s.cfg.QualityLLMBaseURL, "/")+"/chat/completions", bytes.NewReader(payload))
	if err != nil {
		return "", 0, 0, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+key)
	resp, err := (&http.Client{Timeout: 95 * time.Second}).Do(req)
	if err != nil {
		return "", 0, 0, err
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return "", 0, 0, err
	}
	if resp.StatusCode != http.StatusOK {
		return "", 0, 0, fmt.Errorf("llm http %d", resp.StatusCode)
	}
	var envelope struct {
		Choices []struct {
			Message struct {
				Content string `json:"content"`
			} `json:"message"`
		} `json:"choices"`
		Usage struct {
			PromptTokens     int `json:"prompt_tokens"`
			CompletionTokens int `json:"completion_tokens"`
		} `json:"usage"`
	}
	if err := json.Unmarshal(raw, &envelope); err != nil {
		return "", 0, 0, err
	}
	if len(envelope.Choices) == 0 {
		return "", 0, 0, errors.New("empty choices")
	}
	return envelope.Choices[0].Message.Content, envelope.Usage.PromptTokens, envelope.Usage.CompletionTokens, nil
}

const qualityAnalysisSystem = `你是新能源设备预测系统的运维分析助手。只根据给出的事实说话，不编造数据。
输出严格的 JSON：{"conclusion": "一句话结论", "reasons": ["依据1","依据2"], "suggestions": ["建议1"]}。
结论必须落到「是模型退化、数据不足、还是形态滞后」三者之一；建议必须可执行（观察/重训/调阈值/回退）。
不要输出 Markdown，不要输出 JSON 以外的内容。`

// predictionQualityAnalysis POST /predictions/quality/analysis
func (s *Server) predictionQualityAnalysis(c *gin.Context) {
	var body analysisRequest
	if err := c.ShouldBindJSON(&body); err != nil {
		fail(c, 400, 40001, "invalid analysis payload")
		return
	}
	if body.Kind != "drift" && body.Kind != "trial" {
		fail(c, 400, 40001, "kind must be drift or trial")
		return
	}
	ctx := c.Request.Context()
	facts, err := s.collectAnalysisFacts(ctx, body.Kind)
	if errors.Is(err, gorm.ErrRecordNotFound) {
		fail(c, 409, 40901, "当前没有试用中的版本可对比")
		return
	}
	if err != nil {
		fail(c, 503, 50000, "analysis facts unavailable")
		return
	}
	encoded, _ := json.Marshal(facts)
	content, promptTokens, completionTokens, err := s.callQualityLLM(
		ctx, qualityAnalysisSystem, "事实如下（JSON）：\n"+string(encoded))
	if err != nil {
		fail(c, 503, 50302, "AI 解读暂不可用："+err.Error())
		return
	}
	var parsed struct {
		Conclusion  string   `json:"conclusion"`
		Reasons     []string `json:"reasons"`
		Suggestions []string `json:"suggestions"`
	}
	trimmed := strings.TrimSpace(content)
	trimmed = strings.TrimPrefix(trimmed, "```json")
	trimmed = strings.TrimPrefix(trimmed, "```")
	trimmed = strings.TrimSuffix(trimmed, "```")
	trimmed = strings.TrimSpace(trimmed)
	// 开 thinking 时模型偶尔会在 JSON 前后加一句说明，截出最外层大括号再解析。
	if start := strings.Index(trimmed, "{"); start >= 0 {
		if end := strings.LastIndex(trimmed, "}"); end > start {
			trimmed = trimmed[start : end+1]
		}
	}
	if err := json.Unmarshal([]byte(trimmed), &parsed); err != nil {
		preview := trimmed
		if len(preview) > 180 {
			preview = preview[:180] + "…"
		}
		fail(c, 502, 50202, "AI 输出无法解析："+preview)
		return
	}
	reasonsJSON, _ := json.Marshal(parsed.Reasons)
	suggestJSON, _ := json.Marshal(parsed.Suggestions)
	var id int64
	if err := s.db.WithContext(ctx).Raw(`
		INSERT INTO prediction_quality_analysis(kind, subject, conclusion, reasons, suggestions, model, prompt_version, prompt_tokens, completion_tokens)
		VALUES(?,?,?,?::jsonb,?::jsonb,?,?,?,?) RETURNING id`,
		body.Kind, facts.Subject, parsed.Conclusion, string(reasonsJSON), string(suggestJSON),
		s.cfg.QualityLLMModel, qualityAnalysisPromptVersion, promptTokens, completionTokens).Scan(&id).Error; err != nil {
		fail(c, 503, 50000, "analysis persist failed")
		return
	}
	ok(c, analysisResult{
		ID: id, Kind: body.Kind, Subject: facts.Subject,
		Conclusion: parsed.Conclusion, Reasons: parsed.Reasons, Suggestions: parsed.Suggestions,
		PromptTokens: promptTokens, CompletionTokens: completionTokens,
		CreatedAt: time.Now().UTC().Format(time.RFC3339),
	})
}

// predictionQualityAnalysisLatest GET /predictions/quality/analysis?kind=drift
func (s *Server) predictionQualityAnalysisLatest(c *gin.Context) {
	kind := c.DefaultQuery("kind", "drift")
	if kind != "drift" && kind != "trial" {
		fail(c, 400, 40001, "kind must be drift or trial")
		return
	}
	var row struct {
		ID               int64
		Kind             string
		Subject          string
		Conclusion       string
		Reasons          []byte
		Suggestions      []byte
		PromptTokens     int
		CompletionTokens int
		CreatedAt        time.Time
	}
	err := s.db.WithContext(c.Request.Context()).Raw(`
		SELECT id, kind, subject, conclusion, reasons, suggestions, prompt_tokens, completion_tokens, created_at
		FROM prediction_quality_analysis WHERE kind = ? ORDER BY id DESC LIMIT 1`, kind).Scan(&row).Error
	if err != nil || row.ID == 0 {
		ok(c, gin.H{"items": []any{}})
		return
	}
	var reasons, suggestions []string
	_ = json.Unmarshal(row.Reasons, &reasons)
	_ = json.Unmarshal(row.Suggestions, &suggestions)
	ok(c, gin.H{"items": []any{analysisResult{
		ID: row.ID, Kind: row.Kind, Subject: row.Subject, Conclusion: row.Conclusion,
		Reasons: reasons, Suggestions: suggestions,
		PromptTokens: row.PromptTokens, CompletionTokens: row.CompletionTokens,
		CreatedAt: row.CreatedAt.UTC().Format(time.RFC3339),
	}}})
}

// 编译期占位：避免 strconv 在后续扩展时被误删
var _ = strconv.Itoa
