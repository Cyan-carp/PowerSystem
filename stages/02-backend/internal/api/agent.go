package api

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
)

type agentChatInput struct {
	Message   string `json:"message"`
	SessionID string `json:"session_id,omitempty"`
}

func (s *Server) agentChat(c *gin.Context) {
	if !s.cfg.AgentEnabled || s.cfg.AgentToken == "" {
		fail(c, 503, 50301, "agent unavailable or disabled")
		return
	}
	var input agentChatInput
	decoder := json.NewDecoder(http.MaxBytesReader(c.Writer, c.Request.Body, 32768))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&input) != nil || decoder.Decode(new(any)) != io.EOF || strings.TrimSpace(input.Message) == "" || len([]rune(input.Message)) > 4000 || (input.SessionID != "" && !validSessionID(input.SessionID)) {
		fail(c, 400, 40001, "invalid agent request")
		return
	}
	admissionCtx, admissionCancel := context.WithTimeout(c.Request.Context(), 2*time.Second)
	release, retry, admissionErr := s.admitChat(admissionCtx, c.GetInt64("user_id"))
	admissionCancel()
	if admissionErr != nil {
		fail(c, 503, 50301, "问答调用保护暂不可用，请稍后重试")
		return
	}
	if retry > 0 {
		c.Header("Retry-After", strconv.Itoa(retry))
		fail(c, 429, 42901, "问答请求已达限制，请稍后重试")
		return
	}
	defer release()
	body, _ := json.Marshal(struct {
		agentChatInput
		UserID int64 `json:"user_id"`
	}{input, c.GetInt64("user_id")})
	ctx, cancel := context.WithTimeout(c.Request.Context(), 45*time.Second)
	defer cancel()
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, s.cfg.AgentURL+"/internal/agent/chat", bytes.NewReader(body))
	if err != nil {
		fail(c, 503, 50301, "agent unavailable")
		return
	}
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Authorization", c.GetHeader("Authorization"))
	request.Header.Set("X-PowerSystem-Token", s.cfg.AgentToken)
	client := &http.Client{Timeout: 45 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	result, err := client.Do(request)
	if err != nil {
		fail(c, 503, 50301, "agent unavailable")
		return
	}
	defer result.Body.Close()
	if result.StatusCode != http.StatusOK {
		switch result.StatusCode {
		case 403:
			fail(c, 403, 40301, "agent access denied")
		case 409:
			fail(c, 409, 40901, "agent session busy")
		case 422:
			fail(c, 400, 40001, "invalid agent request")
		default:
			fail(c, 503, 50301, "agent unavailable")
		}
		return
	}
	raw, err := io.ReadAll(io.LimitReader(result.Body, (256<<10)+1))
	var data map[string]any
	if err != nil || len(raw) > 256<<10 || json.Unmarshal(raw, &data) != nil || data["request_id"] == nil || data["session_id"] == nil || (data["status"] != "answered" && data["status"] != "degraded" && data["status"] != "unable_to_determine") {
		fail(c, 503, 50301, "invalid agent response")
		return
	}
	ok(c, data)
}

func validSessionID(value string) bool {
	if len(value) != 36 {
		return false
	}
	for i, character := range value {
		if i == 8 || i == 13 || i == 18 || i == 23 {
			if character != '-' {
				return false
			}
		} else if !strings.ContainsRune("0123456789abcdefABCDEF", character) {
			return false
		}
	}
	return true
}

var knowledgeID = regexp.MustCompile(`^K[a-f0-9]{20}$`)

func (s *Server) agentKnowledge(c *gin.Context) {
	if !s.cfg.AgentEnabled || s.cfg.AgentToken == "" {
		fail(c, 503, 50301, "agent unavailable or disabled")
		return
	}
	id := c.Param("id")
	if !knowledgeID.MatchString(id) {
		fail(c, 400, 40001, "invalid knowledge source")
		return
	}
	ctx, cancel := context.WithTimeout(c.Request.Context(), 5*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, s.cfg.AgentURL+"/internal/agent/knowledge/"+id, nil)
	if err != nil {
		fail(c, 503, 50301, "agent unavailable")
		return
	}
	req.Header.Set("X-PowerSystem-Token", s.cfg.AgentToken)
	client := &http.Client{Timeout: 5 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	result, err := client.Do(req)
	if err != nil {
		fail(c, 503, 50301, "knowledge unavailable")
		return
	}
	defer result.Body.Close()
	if result.StatusCode == 404 {
		fail(c, 404, 40401, "knowledge source not found")
		return
	}
	var source map[string]any
	raw, err := io.ReadAll(io.LimitReader(result.Body, 65537))
	if result.StatusCode != 200 || err != nil || len(raw) > 65536 || json.Unmarshal(raw, &source) != nil || source["id"] != id {
		fail(c, 503, 50301, "invalid knowledge response")
		return
	}
	ok(c, source)
}
