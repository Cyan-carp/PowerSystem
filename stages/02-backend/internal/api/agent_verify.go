package api

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"io"
	"net/http"
	"strconv"
	"time"

	"github.com/gin-gonic/gin"
)

type agentVerifyInput struct {
	ParentTurnID   int64    `json:"parent_turn_id"`
	DeviceID       int64    `json:"device_id"`
	Checks         []string `json:"checks"`
	VerificationID string   `json:"verification_id"`
}

func (s *Server) agentRAGVerify(c *gin.Context) {
	if !s.cfg.AgentEnabled || s.cfg.AgentChatURL == "" || s.cfg.AgentToken == "" {
		fail(c, 503, 50301, "核验服务不可用")
		return
	}
	var input agentVerifyInput
	decoder := json.NewDecoder(http.MaxBytesReader(c.Writer, c.Request.Body, 256<<10))
	decoder.DisallowUnknownFields()
	if !validSessionID(c.Param("id")) || decoder.Decode(&input) != nil || decoder.Decode(new(any)) != io.EOF || input.ParentTurnID <= 0 || input.DeviceID <= 0 || !validSessionID(input.VerificationID) || len(input.Checks) < 1 || len(input.Checks) > 3 {
		fail(c, 400, 40001, "核验参数无效")
		return
	}
	for _, check := range input.Checks {
		if check != "latest_telemetry" && check != "current_alarms" && check != "prediction" {
			fail(c, 400, 40001, "核验项目无效")
			return
		}
	}
	ctx, cancel := context.WithTimeout(c.Request.Context(), 2*time.Second)
	release, retry, err := s.admitChat(ctx, c.GetInt64("user_id"))
	cancel()
	if err != nil {
		fail(c, 503, 50301, "调用保护暂不可用")
		return
	}
	if retry > 0 {
		c.Header("Retry-After", strconv.Itoa(retry))
		fail(c, 429, 42901, "请求已达限制，请稍后重试")
		return
	}
	defer release()
	body, _ := json.Marshal(struct {
		agentVerifyInput
		UserID int64 `json:"user_id"`
	}{input, c.GetInt64("user_id")})
	ctx, cancel = context.WithTimeout(c.Request.Context(), s.chatTimeout())
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, s.cfg.AgentChatURL+"/internal/agent/sessions/"+c.Param("id")+"/verify", bytes.NewReader(body))
	if err != nil {
		fail(c, 503, 50301, "核验服务不可用")
		return
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", c.GetHeader("Authorization"))
	req.Header.Set("X-PowerSystem-Token", s.cfg.AgentToken)
	if s.cfg.OpsURL != "" {
		req.Header.Set("X-MA-Actor", s.maContext(c, c.Param("id"), string(body)))
		req.Header.Set("X-MA-Request", base64.RawURLEncoding.EncodeToString(body))
	}
	client := &http.Client{Timeout: s.chatTimeout(), CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	resp, err := client.Do(req)
	if err != nil {
		fail(c, 503, 50301, "核验未完成；请沿用原请求重试")
		return
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		switch resp.StatusCode {
		case 400, 422:
			fail(c, 400, 40001, "核验参数或设备无效")
		case 401, 403:
			fail(c, 403, 40301, "核验访问被拒绝")
		case 404:
			fail(c, 404, 40401, "会话或原回答不存在")
		case 409:
			fail(c, 409, 40901, "会话忙或核验请求冲突")
		default:
			fail(c, 503, 50301, "核验服务不可用；请沿用原请求重试")
		}
		return
	}
	raw, err := io.ReadAll(io.LimitReader(resp.Body, (8<<20)+1))
	var data map[string]any
	if err != nil || len(raw) > 8<<20 || json.Unmarshal(raw, &data) != nil || data["kind"] != "verification" || data["turn_id"] == nil || data["session_id"] != c.Param("id") {
		fail(c, 503, 50301, "核验响应无效")
		return
	}
	ok(c, data)
}
