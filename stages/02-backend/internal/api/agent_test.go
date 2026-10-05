package api

import (
	"encoding/json"
	"github.com/gin-gonic/gin"
	"net/http"
	"net/http/httptest"
	"powersystem/backend/internal/config"
	"strings"
	"testing"
)

func agentRouter(s *Server) *gin.Engine {
	r := gin.New()
	r.POST("/agent", func(c *gin.Context) { c.Set("user_id", int64(17)) }, s.agentChat)
	return r
}

func TestAgentProxyIdentity(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer user-token" || r.Header.Get("X-PowerSystem-Token") != "private-service-token" {
			t.Error("credentials not propagated")
		}
		var body map[string]any
		if json.NewDecoder(r.Body).Decode(&body) != nil || body["user_id"] != float64(17) {
			t.Error("identity must come from context")
		}
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"request_id":"r","session_id":"s","status":"degraded"}`))
	}))
	defer upstream.Close()
	s := &Server{cfg: config.Config{AgentEnabled: true, AgentURL: upstream.URL, AgentToken: "private-service-token"}}
	r := agentRouter(s)
	for _, tc := range []struct {
		body   string
		status int
	}{
		{`{"message":"查询设备"}`, 200},
		{`{"message":"查询设备","user_id":99}`, 400},
		{`{"message":" "}`, 400},
		{`{"message":"x","session_id":"not-uuid"}`, 400},
		{`{"message":"x"}{}`, 400},
	} {
		w := httptest.NewRecorder()
		req := httptest.NewRequest("POST", "/agent", strings.NewReader(tc.body))
		req.Header.Set("Authorization", "Bearer user-token")
		r.ServeHTTP(w, req)
		if w.Code != tc.status {
			t.Errorf("%s: got %d want %d", tc.body, w.Code, tc.status)
		}
	}
}

func TestAgentDisabledAndFailure(t *testing.T) {
	for _, cfg := range []config.Config{{}, {AgentEnabled: true, AgentURL: "http://127.0.0.1:1", AgentToken: "private-token"}} {
		w := httptest.NewRecorder()
		agentRouter(&Server{cfg: cfg}).ServeHTTP(w, httptest.NewRequest("POST", "/agent", strings.NewReader(`{"message":"query"}`)))
		if w.Code != 503 {
			t.Errorf("got %d want 503", w.Code)
		}
	}
}

func TestAgentRedirectDoesNotForwardCredentials(t *testing.T) {
	called := false
	destination := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { called = true }))
	defer destination.Close()
	redirect := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { http.Redirect(w, r, destination.URL, 307) }))
	defer redirect.Close()
	w := httptest.NewRecorder()
	agentRouter(&Server{cfg: config.Config{AgentEnabled: true, AgentURL: redirect.URL, AgentToken: "private"}}).ServeHTTP(w, httptest.NewRequest("POST", "/agent", strings.NewReader(`{"message":"query"}`)))
	if called || w.Code != 503 {
		t.Fatal("redirect must fail without forwarding credentials")
	}
}

func TestKnowledgeProxyClosedIDs(t *testing.T) {
	id := "K0123456789abcdef0123"
	called := 0
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		called++
		if r.Method != "GET" || r.URL.Path != "/internal/agent/knowledge/"+id || r.Header.Get("X-PowerSystem-Token") != "private" {
			t.Error("knowledge proxy must use a fixed internal path and service token")
		}
		w.Write([]byte(`{"id":"` + id + `","heading":"guide","content":"read only"}`))
	}))
	defer upstream.Close()
	s := &Server{cfg: config.Config{AgentEnabled: true, AgentURL: upstream.URL, AgentToken: "private"}}
	r := gin.New()
	r.GET("/knowledge/:id", s.agentKnowledge)
	for _, tc := range []struct {
		id   string
		code int
	}{{id, 200}, {"invalid", 400}, {"K0000000000000000000G", 400}} {
		w := httptest.NewRecorder()
		r.ServeHTTP(w, httptest.NewRequest("GET", "/knowledge/"+tc.id, nil))
		if w.Code != tc.code {
			t.Errorf("%s got %d", tc.id, w.Code)
		}
	}
	if called != 1 {
		t.Fatal("invalid IDs must never reach upstream")
	}
}

func TestKnowledgeRedirectRejected(t *testing.T) {
	called := false
	destination := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { called = true }))
	defer destination.Close()
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { http.Redirect(w, r, destination.URL, 307) }))
	defer upstream.Close()
	s := &Server{cfg: config.Config{AgentEnabled: true, AgentURL: upstream.URL, AgentToken: "private"}}
	r := gin.New()
	r.GET("/knowledge/:id", s.agentKnowledge)
	w := httptest.NewRecorder()
	r.ServeHTTP(w, httptest.NewRequest("GET", "/knowledge/K0123456789abcdef0123", nil))
	if called || w.Code != 503 {
		t.Fatal("redirect must not forward service token")
	}
}
