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

func TestRAGProxyUsesNewChatAndPreservesOldService(t *testing.T) {
	oldCalls := 0
	old := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		oldCalls++
		w.Write([]byte(`{"id":"K0123456789abcdef0123","heading":"old","content":"legacy"}`))
	}))
	defer old.Close()
	next := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-PowerSystem-Token") != "private" {
			t.Error("missing internal authentication")
		}
		if r.URL.Path == "/internal/agent/chat" {
			w.Write([]byte(`{"request_id":"r","session_id":"s","status":"answered","answer_markdown":"生活答案"}`))
			return
		}
		if strings.HasPrefix(r.URL.Path, "/internal/agent/sessions") && r.URL.Query().Get("user_id") != "17" {
			t.Error("caller identity must be injected")
		}
		w.Write([]byte(`{"list":[]}`))
	}))
	defer next.Close()
	s := &Server{cfg: config.Config{AgentEnabled: true, AgentURL: old.URL, AgentChatURL: next.URL, AgentToken: "private"}}
	r := agentRouter(s)
	r.GET("/sessions", func(c *gin.Context) { c.Set("user_id", int64(17)) }, s.agentRAGProxy)
	r.GET("/knowledge/:id", s.agentKnowledge)
	w := httptest.NewRecorder()
	r.ServeHTTP(w, httptest.NewRequest("POST", "/agent", strings.NewReader(`{"message":"炒蛋"}`)))
	var value map[string]any
	json.Unmarshal(w.Body.Bytes(), &value)
	if w.Code != 200 || !strings.Contains(w.Body.String(), "生活答案") || oldCalls != 0 {
		t.Fatal("new chat routing failed")
	}
	w = httptest.NewRecorder()
	r.ServeHTTP(w, httptest.NewRequest("GET", "/sessions?user_id=999", nil))
	if w.Code != 200 {
		t.Fatal("session proxy failed")
	}
	w = httptest.NewRecorder()
	r.ServeHTTP(w, httptest.NewRequest("GET", "/knowledge/K0123456789abcdef0123", nil))
	if w.Code != 200 || oldCalls != 1 {
		t.Fatal("legacy knowledge routing changed")
	}
}

func TestRAGSourcePinsHistoricalCollectionAndVersion(t *testing.T) {
	calls := 0
	next := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if r.URL.Query().Get("collection") != "powersystem_rag_old_import" || r.URL.Query().Get("version") != strings.Repeat("a", 64) {
			t.Errorf("lost source version: %s", r.URL.RawQuery)
		}
		w.Write([]byte(`{"content":"retained source"}`))
	}))
	defer next.Close()
	s := &Server{cfg: config.Config{AgentEnabled: true, AgentChatURL: next.URL, AgentToken: "fixture"}}
	gin.SetMode(gin.TestMode)
	router := gin.New()
	router.GET("/rag/sources/:id", s.agentRAGProxy)
	base := "/rag/sources/K0123456789abcdef0123"
	w := httptest.NewRecorder()
	router.ServeHTTP(w, httptest.NewRequest("GET", base+"?collection=powersystem_rag_old_import&version="+strings.Repeat("a", 64), nil))
	if w.Code != 200 || calls != 1 {
		t.Fatalf("pin request failed: %d", w.Code)
	}
	w = httptest.NewRecorder()
	router.ServeHTTP(w, httptest.NewRequest("GET", base+"?collection=unrelated_private", nil))
	if w.Code != 400 || calls != 1 {
		t.Fatalf("bad collection forwarded: %d", w.Code)
	}
}

func TestRAGSessionArchiveRoutesAndIdentity(t *testing.T) {
	const sid = "c1c2f3b4-17a3-4bb1-9988-8b8cc7a7f211"
	var paths []string
	next := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Query().Get("user_id") != "17" || r.Header.Get("X-PowerSystem-Token") != "fixture" {
			t.Error("archive identity must come from authenticated context")
		}
		paths = append(paths, r.Method+" "+r.URL.Path)
		if r.Method == "GET" && r.URL.Query().Get("archived") != "true" {
			t.Error("archive list filter was lost")
		}
		w.Write([]byte(`{"archived":true,"list":[]}`))
	}))
	defer next.Close()
	s := &Server{cfg: config.Config{AgentEnabled: true, AgentChatURL: next.URL, AgentToken: "fixture"}}
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.Use(func(c *gin.Context) { c.Set("user_id", int64(17)) })
	r.GET("/agent/sessions", s.agentRAGProxy)
	r.POST("/agent/sessions/:id/archive", s.agentRAGProxy)
	r.POST("/agent/sessions/:id/restore", s.agentRAGProxy)
	for _, action := range []string{"archive", "restore"} {
		w := httptest.NewRecorder()
		r.ServeHTTP(w, httptest.NewRequest("POST", "/agent/sessions/"+sid+"/"+action+"?user_id=999", nil))
		if w.Code != 200 || paths[len(paths)-1] != "POST /internal/agent/sessions/"+sid+"/"+action {
			t.Fatalf("archive action not forwarded: %d %v", w.Code, paths)
		}
	}
	w := httptest.NewRecorder()
	r.ServeHTTP(w, httptest.NewRequest("GET", "/agent/sessions?archived=true&user_id=999", nil))
	if w.Code != 200 {
		t.Fatal("archived listing failed")
	}
	for _, path := range []string{"/agent/sessions?archived=anything", "/agent/sessions?offset=-1"} {
		w = httptest.NewRecorder()
		r.ServeHTTP(w, httptest.NewRequest("GET", path, nil))
		if w.Code != 400 || len(paths) != 3 {
			t.Fatal("invalid query forwarded")
		}
	}
}
