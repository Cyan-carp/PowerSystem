package api

import (
	"bufio"
	"encoding/base64"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gin-gonic/gin"

	"powersystem/backend/internal/config"
)

const (
	runIDFixture       = "6f1d1f1a-0b6e-4c2f-9d3a-1c2b3a4d5e6f"
	sessionIDFixture   = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
	idempotencyFixture = "11111111-2222-4333-8444-555555555555"
)

func newAgentRunsRouter(s *Server, userID int64) *gin.Engine {
	gin.SetMode(gin.TestMode)
	identity := func(c *gin.Context) { c.Set("user_id", userID) }
	r := gin.New()
	r.POST("/agent/runs", identity, s.agentRunsProxy)
	r.GET("/agent/runs/:id", identity, s.agentRunsProxy)
	r.GET("/agent/runs/:id/events", identity, s.agentRunsProxy)
	r.POST("/agent/runs/:id/cancel", identity, s.agentRunsProxy)
	return r
}

func TestAgentRunSubmitInjectsIdentityAndNeverDoubleSendsLegacyChat(t *testing.T) {
	var calls, legacyCalls int
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if r.URL.Path == "/internal/agent/chat" {
			legacyCalls++
			t.Error("legacy chat must not be called by the runs protocol")
		}
		if r.URL.Path != "/internal/agent/runs" || r.Method != http.MethodPost {
			t.Errorf("unexpected upstream route: %s %s", r.Method, r.URL.Path)
		}
		if r.Header.Get("X-PowerSystem-Token") != "service-token" {
			t.Error("service token not attached")
		}
		if r.Header.Get("Authorization") != "Bearer user-jwt" {
			t.Error("caller authorization not relayed")
		}
		if r.Header.Get("Idempotency-Key") != idempotencyFixture {
			t.Errorf("idempotency key not forwarded: %q", r.Header.Get("Idempotency-Key"))
		}
		if r.Header.Get("X-MA-Actor") != "" {
			t.Error("gateway must not relay a client supplied actor")
		}
		var body map[string]any
		if json.NewDecoder(r.Body).Decode(&body) != nil {
			t.Error("unreadable upstream body")
		}
		if body["user_id"] != float64(17) {
			t.Errorf("identity must come from JWT context: %v", body["user_id"])
		}
		if body["message"] != "查一下告警" || body["session_id"] != sessionIDFixture {
			t.Errorf("request payload changed: %v", body)
		}
		context, ok := body["page_context"].(map[string]any)
		if !ok || context["page"] != "dashboard" {
			t.Errorf("page context not forwarded: %v", body["page_context"])
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusAccepted)
		w.Write([]byte(`{"protocol":"assistant/v4-draft1","run_id":"` + runIDFixture + `","turn_id":"` + sessionIDFixture + `","session_id":"` + sessionIDFixture + `","status":"queued"}`))
	}))
	defer upstream.Close()
	s := &Server{cfg: config.Config{AgentEnabled: true, AgentChatURL: upstream.URL, AgentToken: "service-token"}}
	r := newAgentRunsRouter(s, 17)

	// A client supplied user_id is not an accepted field.
	forged := httptest.NewRequest(http.MethodPost, "/agent/runs", strings.NewReader(`{"message":"查一下告警","user_id":999}`))
	forged.Header.Set("Idempotency-Key", idempotencyFixture)
	forged.Header.Set("Authorization", "Bearer user-jwt")
	forged.Header.Set("X-MA-Actor", "forged-actor")
	w := httptest.NewRecorder()
	r.ServeHTTP(w, forged)
	if w.Code != 400 || calls != 0 {
		t.Fatalf("client user_id must be rejected: %d calls=%d", w.Code, calls)
	}

	body := `{"message":"查一下告警","session_id":"` + sessionIDFixture + `","page_context":{"page":"dashboard"}}`
	req := httptest.NewRequest(http.MethodPost, "/agent/runs?user_id=999", strings.NewReader(body))
	req.Header.Set("Idempotency-Key", idempotencyFixture)
	req.Header.Set("Authorization", "Bearer user-jwt")
	req.Header.Set("X-MA-Actor", "forged-actor")
	w = httptest.NewRecorder()
	r.ServeHTTP(w, req)
	if w.Code != http.StatusAccepted {
		t.Fatalf("acceptance status must be preserved: %d %s", w.Code, w.Body.String())
	}
	if !strings.Contains(w.Body.String(), runIDFixture) {
		t.Error("run payload not relayed")
	}
	if calls != 1 || legacyCalls != 0 {
		t.Fatalf("unexpected upstream calls: %d legacy=%d", calls, legacyCalls)
	}
}

func TestAgentRunSubmitValidatesMessageAndIdempotencyKey(t *testing.T) {
	calls := 0
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		w.WriteHeader(http.StatusAccepted)
		w.Write([]byte(`{"run_id":"` + runIDFixture + `"}`))
	}))
	defer upstream.Close()
	s := &Server{cfg: config.Config{AgentEnabled: true, AgentChatURL: upstream.URL, AgentToken: "service-token", AgentChatInputChars: 8}}
	r := newAgentRunsRouter(s, 17)

	cases := []struct {
		name   string
		body   string
		header string
	}{
		{"missing key", `{"message":"hello"}`, ""},
		{"malformed key", `{"message":"hello"}`, "not-a-uuid"},
		{"empty message", `{"message":"   "}`, idempotencyFixture},
		{"message too long", `{"message":"` + strings.Repeat("字", 9) + `"}`, idempotencyFixture},
		{"unknown field", `{"message":"hi","session_id":"` + sessionIDFixture + `","role":"admin"}`, idempotencyFixture},
		{"invalid session", `{"message":"hi","session_id":"../../etc/passwd"}`, idempotencyFixture},
		{"page context not object", `{"message":"hi","page_context":"dashboard"}`, idempotencyFixture},
		{"trailing payload", `{"message":"hi"}{"message":"hi"}`, idempotencyFixture},
	}
	for _, tc := range cases {
		req := httptest.NewRequest(http.MethodPost, "/agent/runs", strings.NewReader(tc.body))
		if tc.header != "" {
			req.Header.Set("Idempotency-Key", tc.header)
		}
		w := httptest.NewRecorder()
		r.ServeHTTP(w, req)
		if w.Code != 400 {
			t.Fatalf("%s: want 400 got %d", tc.name, w.Code)
		}
	}
	if calls != 0 {
		t.Fatalf("invalid requests reached upstream: %d", calls)
	}

	req := httptest.NewRequest(http.MethodPost, "/agent/runs", strings.NewReader(`{"message":"hi"}`))
	req.Header.Set("Idempotency-Key", idempotencyFixture)
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)
	if w.Code != http.StatusAccepted || calls != 1 {
		t.Fatalf("valid request failed: %d", w.Code)
	}
}

// decodeMAActorPayload returns the signed JSON body of an X-MA-Actor value.
func decodeMAActorPayload(t *testing.T, actor string) string {
	t.Helper()
	parts := strings.Split(actor, ".")
	if len(parts) != 2 || parts[0] == "" || parts[1] == "" {
		t.Fatalf("malformed actor: %q", actor)
	}
	raw, err := base64.RawURLEncoding.DecodeString(parts[0])
	if err != nil {
		t.Fatalf("undecodable actor payload: %v", err)
	}
	return string(raw)
}

// Signing is controlled by MAActorKey alone: a new session (session_id omitted)
// is signed too, with session_id "", so the service can validate the actor
// before it creates the session and binds the real sid.
func TestAgentRunSubmitSignsActorForNewSession(t *testing.T) {
	actors := make(chan string, 4)
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		actors <- r.Header.Get("X-MA-Actor")
		w.WriteHeader(http.StatusAccepted)
		w.Write([]byte(`{"run_id":"` + runIDFixture + `"}`))
	}))
	defer upstream.Close()
	// OpsURL is deliberately empty: signing must not depend on it.
	s := &Server{cfg: config.Config{
		AgentEnabled: true, AgentChatURL: upstream.URL, AgentToken: "service-token",
		MAActorKey: strings.Repeat("k", 32),
	}}
	r := newAgentRunsRouter(s, 17)

	send := func(body string) {
		req := httptest.NewRequest(http.MethodPost, "/agent/runs", strings.NewReader(body))
		req.Header.Set("Idempotency-Key", idempotencyFixture)
		req.Header.Set("X-MA-Actor", "forged-actor")
		w := httptest.NewRecorder()
		r.ServeHTTP(w, req)
		if w.Code != http.StatusAccepted {
			t.Fatalf("submit failed: %d", w.Code)
		}
	}

	send(`{"message":"hi","session_id":"` + sessionIDFixture + `"}`)
	actor := <-actors
	if actor == "" || actor == "forged-actor" {
		t.Fatalf("known session must be signed by the gateway: %q", actor)
	}
	if payload := decodeMAActorPayload(t, actor); !strings.Contains(payload, `"session_id":"`+sessionIDFixture+`"`) {
		t.Errorf("actor session not bound: %s", payload)
	}

	send(`{"message":"hi"}`)
	actor = <-actors
	if actor == "" || actor == "forged-actor" {
		t.Fatalf("new session must be signed by the gateway: %q", actor)
	}
	if payload := decodeMAActorPayload(t, actor); !strings.Contains(payload, `"session_id":""`) {
		t.Errorf("new session actor must carry an empty session_id: %s", payload)
	}
}

func TestAgentRunSnapshotInjectsQueryIdentityAndPreservesStatus(t *testing.T) {
	type seen struct {
		path, method, userID string
	}
	requests := make(chan seen, 8)
	status := http.StatusOK
	payload := `{"protocol":"assistant/v4-draft1","run_id":"` + runIDFixture + `","status":"running","last_seq":7}`
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests <- seen{r.URL.Path, r.Method, r.URL.Query().Get("user_id")}
		if r.Header.Get("X-PowerSystem-Token") != "service-token" {
			t.Error("service token missing")
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		w.Write([]byte(payload))
	}))
	defer upstream.Close()
	s := &Server{cfg: config.Config{AgentEnabled: true, AgentChatURL: upstream.URL, AgentToken: "service-token"}}
	r := newAgentRunsRouter(s, 17)

	w := httptest.NewRecorder()
	r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/agent/runs/"+runIDFixture+"?user_id=999", nil))
	if w.Code != 200 || !strings.Contains(w.Body.String(), `"last_seq":7`) {
		t.Fatalf("snapshot failed: %d %s", w.Code, w.Body.String())
	}
	if got := <-requests; got.userID != "17" || got.path != "/internal/agent/runs/"+runIDFixture || got.method != http.MethodGet {
		t.Fatalf("identity or path wrong: %+v", got)
	}

	w = httptest.NewRecorder()
	r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/agent/runs/../../internal/agent/chat", nil))
	if w.Code != 404 && w.Code != 400 {
		t.Fatalf("non UUID run path must not be proxied: %d", w.Code)
	}

	for _, code := range []int{400, 404, 409, 410, 422, 503} {
		status = code
		payload = `{"error":{"code":"stable_code"}}`
		w = httptest.NewRecorder()
		r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/agent/runs/"+runIDFixture, nil))
		if w.Code != code || !strings.Contains(w.Body.String(), "stable_code") {
			t.Fatalf("status %d not preserved: %d %s", code, w.Code, w.Body.String())
		}
	}

	status = http.StatusInternalServerError
	payload = `{"error":"boom"}`
	w = httptest.NewRecorder()
	r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/agent/runs/"+runIDFixture, nil))
	if w.Code != 503 {
		t.Fatalf("unexpected upstream status must become 503: %d", w.Code)
	}

	status = http.StatusOK
	payload = `not json`
	w = httptest.NewRecorder()
	r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/agent/runs/"+runIDFixture, nil))
	if w.Code != 503 {
		t.Fatalf("malformed success body must become 503: %d", w.Code)
	}
}

func TestAgentRunSnapshotEnforcesResponseLimit(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(`{"pad":"` + strings.Repeat("a", (8<<20)+16) + `"}`))
	}))
	defer upstream.Close()
	s := &Server{cfg: config.Config{AgentEnabled: true, AgentChatURL: upstream.URL, AgentToken: "service-token"}}
	r := newAgentRunsRouter(s, 17)
	w := httptest.NewRecorder()
	r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/agent/runs/"+runIDFixture, nil))
	if w.Code != 503 {
		t.Fatalf("oversized response must be refused: %d", w.Code)
	}
}

func TestAgentRunEventsForwardsCursorAndStreams(t *testing.T) {
	requests := make(chan *http.Request, 4)
	var calls int64
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt64(&calls, 1)
		requests <- r.Clone(r.Context())
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(http.StatusOK)
		w.Write([]byte("id: 42\nevent: progress\ndata: {\"phase\":\"retrieval\"}\n\n"))
		w.(http.Flusher).Flush()
		w.Write([]byte("id: 43\nevent: completed\ndata: {\"status\":\"succeeded\"}\n\n"))
	}))
	defer upstream.Close()
	s := &Server{cfg: config.Config{AgentEnabled: true, AgentChatURL: upstream.URL, AgentToken: "service-token"}}
	r := newAgentRunsRouter(s, 17)

	req := httptest.NewRequest(http.MethodGet, "/agent/runs/"+runIDFixture+"/events?after_seq=41&user_id=999", nil)
	req.Header.Set("Last-Event-ID", "41")
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)
	if w.Code != 200 {
		t.Fatalf("stream failed: %d %s", w.Code, w.Body.String())
	}
	if !strings.HasPrefix(w.Header().Get("Content-Type"), "text/event-stream") {
		t.Fatalf("content type not relayed: %q", w.Header().Get("Content-Type"))
	}
	if !strings.Contains(w.Body.String(), "id: 42") || !strings.Contains(w.Body.String(), "id: 43") {
		t.Fatalf("events not relayed: %s", w.Body.String())
	}
	got := <-requests
	if got.URL.Path != "/internal/agent/runs/"+runIDFixture+"/events" {
		t.Errorf("unexpected events path: %s", got.URL.Path)
	}
	if got.URL.Query().Get("user_id") != "17" || got.URL.Query().Get("after_seq") != "41" {
		t.Errorf("cursor or identity lost: %s", got.URL.RawQuery)
	}
	if got.Header.Get("Last-Event-ID") != "" {
		t.Error("cursor must be merged into after_seq, not duplicated")
	}
	if got.Header.Get("Accept") != "text/event-stream" {
		t.Errorf("accept header wrong: %q", got.Header.Get("Accept"))
	}

	// Conflicting cursors are a client error and never reach the service.
	req = httptest.NewRequest(http.MethodGet, "/agent/runs/"+runIDFixture+"/events?after_seq=41", nil)
	req.Header.Set("Last-Event-ID", "42")
	w = httptest.NewRecorder()
	r.ServeHTTP(w, req)
	if w.Code != 400 {
		t.Fatalf("conflicting cursor must be 400: %d", w.Code)
	}

	// Invalid cursors are rejected rather than silently restarted. The contract
	// allows only an ASCII decimal non-negative sequence, so "+1" and an
	// explicitly empty after_seq are client errors.
	for _, target := range []string{
		"/agent/runs/" + runIDFixture + "/events?after_seq=abc",
		"/agent/runs/" + runIDFixture + "/events?after_seq=-1",
		"/agent/runs/" + runIDFixture + "/events?after_seq=%2B1",
		"/agent/runs/" + runIDFixture + "/events?after_seq=",
		"/agent/runs/" + runIDFixture + "/events?after_seq=%2041",
		"/agent/runs/not-a-uuid/events",
	} {
		w = httptest.NewRecorder()
		r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, target, nil))
		if w.Code != 400 {
			t.Fatalf("%s: want 400 got %d", target, w.Code)
		}
	}
	if atomic.LoadInt64(&calls) != 1 {
		t.Fatalf("invalid cursors reached the service: %d", atomic.LoadInt64(&calls))
	}
}

func TestAgentRunEventsPreservesExpiredCursorStatus(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusGone)
		w.Write([]byte(`{"error":{"code":"event_cursor_expired"}}`))
	}))
	defer upstream.Close()
	s := &Server{cfg: config.Config{AgentEnabled: true, AgentChatURL: upstream.URL, AgentToken: "service-token"}}
	r := newAgentRunsRouter(s, 17)
	w := httptest.NewRecorder()
	r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/agent/runs/"+runIDFixture+"/events?after_seq=1", nil))
	if w.Code != http.StatusGone || !strings.Contains(w.Body.String(), "event_cursor_expired") {
		t.Fatalf("expired cursor must stay 410: %d %s", w.Code, w.Body.String())
	}
}

func TestAgentRunEventsStreamsIncrementally(t *testing.T) {
	release := make(chan struct{})
	var once sync.Once
	stop := func() { once.Do(func() { close(release) }) }
	defer stop()
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(http.StatusOK)
		w.Write([]byte("id: 1\nevent: accepted\ndata: {}\n\n"))
		w.(http.Flusher).Flush()
		<-release
	}))
	defer upstream.Close()
	s := &Server{cfg: config.Config{AgentEnabled: true, AgentChatURL: upstream.URL, AgentToken: "service-token"}}
	gateway := httptest.NewServer(newAgentRunsRouter(s, 17))
	defer gateway.Close()

	client := &http.Client{Timeout: 5 * time.Second}
	resp, err := client.Get(gateway.URL + "/agent/runs/" + runIDFixture + "/events")
	if err != nil {
		t.Fatalf("stream request failed: %v", err)
	}
	defer resp.Body.Close()
	reader := bufio.NewReader(resp.Body)
	line, err := reader.ReadString('\n')
	if err != nil || line != "id: 1\n" {
		t.Fatalf("first event was buffered instead of flushed: %q %v", line, err)
	}
	stop()
	if _, err := io.Copy(io.Discard, reader); err != nil {
		t.Fatalf("stream close failed: %v", err)
	}
}

func TestAgentRunCancelIsIdempotentAndPreservesStatus(t *testing.T) {
	requests := make(chan *http.Request, 4)
	var calls int64
	status := http.StatusAccepted
	payload := `{"run_id":"` + runIDFixture + `","status":"cancelled","cancel_requested":true}`
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt64(&calls, 1)
		requests <- r.Clone(r.Context())
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		w.Write([]byte(payload))
	}))
	defer upstream.Close()
	s := &Server{cfg: config.Config{AgentEnabled: true, AgentChatURL: upstream.URL, AgentToken: "service-token"}}
	r := newAgentRunsRouter(s, 17)

	w := httptest.NewRecorder()
	r.ServeHTTP(w, httptest.NewRequest(http.MethodPost, "/agent/runs/"+runIDFixture+"/cancel?user_id=999", nil))
	if w.Code != http.StatusAccepted || !strings.Contains(w.Body.String(), "cancel_requested") {
		t.Fatalf("cancel acceptance lost: %d %s", w.Code, w.Body.String())
	}
	got := <-requests
	if got.URL.Path != "/internal/agent/runs/"+runIDFixture+"/cancel" || got.Method != http.MethodPost {
		t.Fatalf("unexpected cancel route: %s %s", got.Method, got.URL.Path)
	}
	if got.URL.Query().Get("user_id") != "17" {
		t.Fatalf("cancel identity not injected: %s", got.URL.RawQuery)
	}

	status = http.StatusConflict
	payload = `{"error":{"code":"cancel_conflict"}}`
	w = httptest.NewRecorder()
	r.ServeHTTP(w, httptest.NewRequest(http.MethodPost, "/agent/runs/"+runIDFixture+"/cancel", nil))
	if w.Code != http.StatusConflict {
		t.Fatalf("conflict must stay 409: %d", w.Code)
	}

	w = httptest.NewRecorder()
	r.ServeHTTP(w, httptest.NewRequest(http.MethodPost, "/agent/runs/"+runIDFixture+"/cancel", strings.NewReader(`{"decision":"approve"}`)))
	if w.Code != 400 {
		t.Fatalf("unexpected cancel body must be rejected: %d", w.Code)
	}
	if atomic.LoadInt64(&calls) != 2 {
		t.Fatalf("rejected cancel reached the service: %d", atomic.LoadInt64(&calls))
	}
}

func TestAgentRunsProxyRequiresEnabledConfiguration(t *testing.T) {
	calls := 0
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls++ }))
	defer upstream.Close()
	for name, cfg := range map[string]config.Config{
		"disabled":        {AgentEnabled: false, AgentChatURL: upstream.URL, AgentToken: "service-token"},
		"missing url":     {AgentEnabled: true, AgentToken: "service-token"},
		"missing service": {AgentEnabled: true, AgentChatURL: upstream.URL},
	} {
		s := &Server{cfg: cfg}
		r := newAgentRunsRouter(s, 17)
		req := httptest.NewRequest(http.MethodPost, "/agent/runs", strings.NewReader(`{"message":"hi"}`))
		req.Header.Set("Idempotency-Key", idempotencyFixture)
		w := httptest.NewRecorder()
		r.ServeHTTP(w, req)
		if w.Code != 503 {
			t.Fatalf("%s: want 503 got %d", name, w.Code)
		}
	}
	if calls != 0 {
		t.Fatalf("disabled proxy reached upstream: %d", calls)
	}
}
