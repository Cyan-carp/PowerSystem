package api

import (
	"context"
	"encoding/json"
	"github.com/gin-gonic/gin"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"time"
)

func (s *Server) chatURL() string {
	if s.cfg.AgentChatURL != "" {
		return s.cfg.AgentChatURL
	}
	return s.cfg.AgentURL
}

func (s *Server) chatTimeout() time.Duration {
	if s.cfg.AgentChatTimeoutSeconds > 0 {
		return time.Duration(s.cfg.AgentChatTimeoutSeconds) * time.Second
	}
	if s.cfg.AgentChatURL != "" {
		return 125 * time.Second
	}
	return 45 * time.Second
}

// Fixed read/history endpoints; identity always comes from JWT middleware.
func (s *Server) agentRAGProxy(c *gin.Context) {
	if !s.cfg.AgentEnabled || s.cfg.AgentChatURL == "" || s.cfg.AgentToken == "" {
		fail(c, 503, 50301, "RAG agent unavailable")
		return
	}
	id := c.Param("id")
	source := strings.Contains(c.FullPath(), "/rag/sources/")
	path := "/internal/agent/sessions"
	if source {
		if !knowledgeID.MatchString(id) {
			fail(c, 400, 40001, "invalid source")
			return
		}
		path = "/internal/agent/rag/sources/" + id
		query := url.Values{}
		for key, pattern := range map[string]string{"collection": `^powersystem_rag_[A-Za-z0-9_-]{1,140}$`, "version": `^[a-f0-9]{64}$`} {
			if value := c.Query(key); value != "" {
				if !regexp.MustCompile(pattern).MatchString(value) {
					fail(c, 400, 40001, "invalid source reference")
					return
				}
				query.Set(key, value)
			}
		}
		if len(query) > 0 {
			path += "?" + query.Encode()
		}
	} else {
		if id != "" {
			if !validSessionID(id) {
				fail(c, 400, 40001, "invalid session")
				return
			}
			path += "/" + id
		}
		path += "?user_id=" + strconv.FormatInt(c.GetInt64("user_id"), 10)
		if id == "" {
			offset, err := strconv.Atoi(c.DefaultQuery("offset", "0"))
			if err != nil || offset < 0 {
				fail(c, 400, 40001, "invalid offset")
				return
			}
			path += "&offset=" + strconv.Itoa(offset)
		}
	}
	ctx, cancel := context.WithTimeout(c.Request.Context(), 15*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, c.Request.Method, s.cfg.AgentChatURL+path, nil)
	if err != nil {
		fail(c, 503, 50301, "agent unavailable")
		return
	}
	req.Header.Set("X-PowerSystem-Token", s.cfg.AgentToken)
	req.Header.Set("Authorization", c.GetHeader("Authorization"))
	if s.cfg.OpsURL != "" {
		req.Header.Set("X-MA-Actor", s.maContext(c, id, ""))
	}
	client := &http.Client{Timeout: 15 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	resp, err := client.Do(req)
	if err != nil {
		fail(c, 503, 50301, "agent unavailable")
		return
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		switch resp.StatusCode {
		case 404:
			fail(c, 404, 40401, "not found")
		case 409:
			fail(c, 409, 40901, "session busy")
		default:
			fail(c, 503, 50301, "agent unavailable")
		}
		return
	}
	raw, err := io.ReadAll(io.LimitReader(resp.Body, (8<<20)+1))
	var value any
	if err != nil || len(raw) > 8<<20 || json.Unmarshal(raw, &value) != nil {
		fail(c, 503, 50301, "invalid agent response")
		return
	}
	ok(c, value)
}
