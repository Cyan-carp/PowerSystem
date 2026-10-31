package api

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"time"

	"github.com/gin-gonic/gin"
)

// 模型通道余额：只在管理员侧展示，且密钥始终留在各自的服务里。
//
// 为什么不在这里直接查供应商：Key 分别由 07（解读）和 08（主助手）持有，
// 复制到 api 容器等于再多一处密钥暴露面。这里只做转发与合并，单条通道失败
// 不能让另一条一起失败——否则一条通道没配就会把整块面板打成「不可用」。

type modelBalanceEntry struct {
	Currency        string `json:"currency"`
	TotalBalance    string `json:"total_balance"`
	GrantedBalance  string `json:"granted_balance"`
	ToppedUpBalance string `json:"topped_up_balance"`
}

// modelBalancePayload 与 07/08 的 /internal/agent/model-balance 返回结构一致。
type modelBalancePayload struct {
	Channel     string              `json:"channel"`
	Label       string              `json:"label"`
	Model       string              `json:"model"`
	BaseURL     string              `json:"base_url"`
	Supported   bool                `json:"supported"`
	IsAvailable *bool               `json:"is_available"`
	Balances    []modelBalanceEntry `json:"balances"`
	FetchedAt   string              `json:"fetched_at"`
	Error       string              `json:"error"`
}

func channelPlaceholder(channel, label, reason string) modelBalancePayload {
	return modelBalancePayload{
		Channel:   channel,
		Label:     label,
		Balances:  []modelBalanceEntry{},
		FetchedAt: time.Now().UTC().Format(time.RFC3339),
		Error:     reason,
	}
}

// getInternal 发一个不带请求体的内部 GET。余额查询是只读的，不发 null body。
func (s *Server) getInternal(ctx context.Context, url string) (map[string]any, error) {
	callCtx, cancel := context.WithTimeout(ctx, 12*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(callCtx, http.MethodGet, url, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Accept", "application/json")
	req.Header.Set("X-PowerSystem-Token", s.cfg.AgentToken)
	resp, err := (&http.Client{
		Timeout:       12 * time.Second,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}).Do(req)
	if err != nil {
		return nil, errors.New("unavailable")
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, errors.New("unavailable")
	}
	payload, err := io.ReadAll(io.LimitReader(resp.Body, (64<<10)+1))
	if err != nil || len(payload) > 64<<10 {
		return nil, errors.New("unavailable")
	}
	var result map[string]any
	if json.Unmarshal(payload, &result) != nil {
		return nil, errors.New("unavailable")
	}
	return result, nil
}

func (s *Server) callChatAgent(ctx context.Context, path string) (map[string]any, error) {
	if !s.cfg.AgentEnabled || s.cfg.AgentChatURL == "" || s.cfg.AgentToken == "" {
		return nil, errors.New("not_configured")
	}
	return s.getInternal(ctx, s.chatURL()+path)
}

// agentModelBalance GET /agent/model-balance —— 两条通道各自查自己的余额。
func (s *Server) agentModelBalance(c *gin.Context) {
	ctx := c.Request.Context()
	channels := make([]modelBalancePayload, 0, 2)

	if raw, err := s.callAgent(ctx, http.MethodGet, "/internal/agent/model-balance", nil); err == nil {
		if encoded, encodeErr := json.Marshal(raw); encodeErr == nil {
			var item modelBalancePayload
			if json.Unmarshal(encoded, &item) == nil {
				if item.Balances == nil {
					item.Balances = []modelBalanceEntry{}
				}
				channels = append(channels, item)
			} else {
				channels = append(channels, channelPlaceholder("interpret", "告警解读", "invalid_response"))
			}
		}
	} else {
		channels = append(channels, channelPlaceholder("interpret", "告警解读", "unavailable"))
	}

	if s.cfg.AgentChatURL == "" {
		channels = append(channels, channelPlaceholder("assistant", "主助手", "not_configured"))
	} else if raw, err := s.callChatAgent(ctx, "/internal/agent/model-balance"); err == nil {
		if encoded, encodeErr := json.Marshal(raw); encodeErr == nil {
			var item modelBalancePayload
			if json.Unmarshal(encoded, &item) == nil {
				if item.Balances == nil {
					item.Balances = []modelBalanceEntry{}
				}
				channels = append(channels, item)
			} else {
				channels = append(channels, channelPlaceholder("assistant", "主助手", "invalid_response"))
			}
		}
	} else {
		channels = append(channels, channelPlaceholder("assistant", "主助手", "unavailable"))
	}

	ok(c, gin.H{"channels": channels, "fetched_at": time.Now().UTC().Format(time.RFC3339)})
}
