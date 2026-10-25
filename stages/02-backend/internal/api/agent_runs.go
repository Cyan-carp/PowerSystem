package api

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
)

// V4-S03 run protocol proxy (assistant/v4-draft1).
//
// Fixed routes, all inside the existing JWT group:
//
//	POST /api/v1/agent/runs
//	GET  /api/v1/agent/runs/:id
//	GET  /api/v1/agent/runs/:id/events
//	POST /api/v1/agent/runs/:id/cancel
//
// The gateway only authenticates, validates and forwards to the fixed internal
// prefix /internal/agent/runs. Identity comes from the JWT middleware and is
// never taken from the client: POST bodies are decoded with DisallowUnknownFields
// so a client-supplied user_id is rejected, and the query user_id is always
// rebuilt from the context. Upstream status codes are preserved (200/202 and
// 400/401/403/404/409/410/422/429/503) instead of being flattened to 200.
const (
	agentRunRequestLimit = 256 << 10
	agentRunJSONLimit    = 8 << 20
	agentRunErrorLimit   = 1 << 20
	agentRunTimeout      = 15 * time.Second
	agentRunSSEWait      = 20 * time.Second
)

var agentRunUUID = regexp.MustCompile(`^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$`)

var (
	agentRunJSONClient = &http.Client{
		Timeout:       agentRunTimeout,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}
	agentRunSSEClient = &http.Client{
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
		Transport: func() http.RoundTripper {
			transport := http.DefaultTransport.(*http.Transport).Clone()
			transport.ResponseHeaderTimeout = agentRunSSEWait
			return transport
		}(),
	}
)

type agentRunInput struct {
	Message     string          `json:"message"`
	SessionID   string          `json:"session_id,omitempty"`
	PageContext json.RawMessage `json:"page_context,omitempty"`
}

func (s *Server) agentRunsProxy(c *gin.Context) {
	if !s.cfg.AgentEnabled || s.cfg.AgentChatURL == "" || s.cfg.AgentToken == "" {
		fail(c, 503, 50301, "agent runs unavailable")
		return
	}
	userID := c.GetInt64("user_id")
	if userID < 1 {
		fail(c, 401, 40101, "missing identity")
		return
	}
	id := c.Param("id")
	path := c.Request.URL.Path
	switch {
	case c.Request.Method == http.MethodPost && id == "" && !strings.HasSuffix(path, "/cancel"):
		s.agentRunSubmit(c, userID)
	case c.Request.Method == http.MethodGet && strings.HasSuffix(path, "/events"):
		s.agentRunEvents(c, userID, id)
	case c.Request.Method == http.MethodPost && strings.HasSuffix(path, "/cancel"):
		s.agentRunCancel(c, userID, id)
	case c.Request.Method == http.MethodGet && id != "":
		s.agentRunSnapshot(c, userID, id)
	default:
		fail(c, 405, 40501, "method not allowed")
	}
}

// agentRunSubmit accepts only message/session_id/page_context. The session is
// created by the internal service when session_id is omitted; the gateway never
// generates one and never double-sends the legacy /internal/agent/chat path.
func (s *Server) agentRunSubmit(c *gin.Context, userID int64) {
	var input agentRunInput
	decoder := json.NewDecoder(http.MaxBytesReader(c.Writer, c.Request.Body, agentRunRequestLimit))
	decoder.DisallowUnknownFields()
	limit := s.cfg.AgentChatInputChars
	if limit <= 0 {
		limit = 32000
	}
	if decoder.Decode(&input) != nil || decoder.Decode(new(any)) != io.EOF {
		fail(c, 400, 40001, "invalid run request")
		return
	}
	if strings.TrimSpace(input.Message) == "" || len([]rune(input.Message)) > limit {
		fail(c, 400, 40001, "invalid run request")
		return
	}
	if input.SessionID != "" && !validSessionID(input.SessionID) {
		fail(c, 400, 40001, "invalid session")
		return
	}
	pageContext := bytes.TrimSpace(input.PageContext)
	if string(pageContext) == "null" {
		pageContext = nil
	}
	if len(pageContext) > 0 && pageContext[0] != '{' {
		fail(c, 400, 40001, "invalid page context")
		return
	}
	key := strings.TrimSpace(c.GetHeader("Idempotency-Key"))
	if !agentRunUUID.MatchString(key) {
		fail(c, 400, 40001, "invalid idempotency key")
		return
	}
	body, err := json.Marshal(struct {
		Message     string          `json:"message"`
		SessionID   string          `json:"session_id,omitempty"`
		PageContext json.RawMessage `json:"page_context,omitempty"`
		UserID      int64           `json:"user_id"`
	}{input.Message, input.SessionID, pageContext, userID})
	if err != nil {
		fail(c, 400, 40001, "invalid run request")
		return
	}
	ctx, cancel := context.WithTimeout(c.Request.Context(), agentRunTimeout)
	defer cancel()
	req, err := s.newAgentRunRequest(ctx, c, http.MethodPost, "/internal/agent/runs", bytes.NewReader(body))
	if err != nil {
		fail(c, 503, 50301, "agent runs unavailable")
		return
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Idempotency-Key", key)
	// The gateway signs the actor context whenever MAActorKey is configured,
	// including a new session (session_id omitted => signed session_id "").
	// The service validates the empty-session signature, creates the session and
	// then binds the real sid to the run; the gateway never calls an identity API
	// and never relays a client supplied X-MA-Actor. Signing depends only on the
	// key, not on whether an ops upstream URL happens to be configured.
	if actor := s.maContext(c, input.SessionID, input.Message); actor != "" {
		req.Header.Set("X-MA-Actor", actor)
	}
	resp, err := agentRunJSONClient.Do(req)
	if err != nil {
		fail(c, 503, 50301, "agent runs unavailable")
		return
	}
	s.proxyAgentRunJSON(c, resp)
}

func (s *Server) agentRunSnapshot(c *gin.Context, userID int64, id string) {
	if !agentRunUUID.MatchString(id) {
		fail(c, 400, 40001, "invalid run")
		return
	}
	ctx, cancel := context.WithTimeout(c.Request.Context(), agentRunTimeout)
	defer cancel()
	req, err := s.newAgentRunRequest(ctx, c, http.MethodGet, agentRunPath(id, "?user_id="+strconv.FormatInt(userID, 10)), nil)
	if err != nil {
		fail(c, 503, 50301, "agent runs unavailable")
		return
	}
	resp, err := agentRunJSONClient.Do(req)
	if err != nil {
		fail(c, 503, 50301, "agent runs unavailable")
		return
	}
	s.proxyAgentRunJSON(c, resp)
}

func (s *Server) agentRunCancel(c *gin.Context, userID int64, id string) {
	if !agentRunUUID.MatchString(id) {
		fail(c, 400, 40001, "invalid run")
		return
	}
	raw, err := io.ReadAll(io.LimitReader(c.Request.Body, 1025))
	if err != nil || len(raw) > 1024 {
		fail(c, 400, 40001, "invalid cancellation")
		return
	}
	switch strings.TrimSpace(string(raw)) {
	case "", "{}":
	default:
		fail(c, 400, 40001, "invalid cancellation")
		return
	}
	ctx, cancel := context.WithTimeout(c.Request.Context(), agentRunTimeout)
	defer cancel()
	req, err := s.newAgentRunRequest(ctx, c, http.MethodPost, agentRunPath(id, "/cancel?user_id="+strconv.FormatInt(userID, 10)), nil)
	if err != nil {
		fail(c, 503, 50301, "agent runs unavailable")
		return
	}
	resp, err := agentRunJSONClient.Do(req)
	if err != nil {
		fail(c, 503, 50301, "agent runs unavailable")
		return
	}
	s.proxyAgentRunJSON(c, resp)
}

// agentRunEvents relays the SSE stream. Last-Event-ID and after_seq are merged
// into a single cursor (conflicting values are rejected with 400), the stream is
// flushed on every upstream chunk so the service-owned 15s heartbeat reaches the
// browser, and a client disconnect only aborts this subscription: the run keeps
// executing upstream because the gateway never issues a cancel.
func (s *Server) agentRunEvents(c *gin.Context, userID int64, id string) {
	if !agentRunUUID.MatchString(id) {
		fail(c, 400, 40001, "invalid run")
		return
	}
	afterSeq, hasAfter := parseAgentRunCursor(c.Query("after_seq"))
	if _, present := c.Request.URL.Query()["after_seq"]; present && !hasAfter {
		fail(c, 400, 40001, "invalid event cursor")
		return
	}
	lastRaw := c.GetHeader("Last-Event-ID")
	lastSeq, hasLast := parseAgentRunCursor(lastRaw)
	if lastRaw != "" && !hasLast {
		fail(c, 400, 40001, "invalid event cursor")
		return
	}
	if hasAfter && hasLast && afterSeq != lastSeq {
		fail(c, 400, 40001, "conflicting event cursor")
		return
	}
	cursor, hasCursor := afterSeq, hasAfter
	if hasLast {
		cursor, hasCursor = lastSeq, true
	}
	query := url.Values{}
	query.Set("user_id", strconv.FormatInt(userID, 10))
	if hasCursor {
		query.Set("after_seq", strconv.FormatInt(cursor, 10))
	}
	req, err := s.newAgentRunRequest(c.Request.Context(), c, http.MethodGet, agentRunPath(id, "/events?"+query.Encode()), nil)
	if err != nil {
		fail(c, 503, 50301, "agent runs unavailable")
		return
	}
	req.Header.Set("Accept", "text/event-stream")
	resp, err := agentRunSSEClient.Do(req)
	if err != nil {
		fail(c, 503, 50301, "agent runs unavailable")
		return
	}
	if resp.StatusCode != http.StatusOK {
		s.proxyAgentRunJSON(c, resp)
		return
	}
	if mediaType := resp.Header.Get("Content-Type"); !strings.HasPrefix(mediaType, "text/event-stream") {
		resp.Body.Close()
		fail(c, 503, 50301, "invalid event stream")
		return
	}
	defer resp.Body.Close()
	header := c.Writer.Header()
	header.Set("Content-Type", "text/event-stream; charset=utf-8")
	header.Set("Cache-Control", "no-cache")
	header.Set("Connection", "keep-alive")
	header.Set("X-Accel-Buffering", "no")
	c.Status(http.StatusOK)
	c.Writer.Flush()
	buffer := make([]byte, 32<<10)
	for {
		n, readErr := resp.Body.Read(buffer)
		if n > 0 {
			if _, writeErr := c.Writer.Write(buffer[:n]); writeErr != nil {
				return
			}
			c.Writer.Flush()
		}
		if readErr != nil {
			return
		}
	}
}

// newAgentRunRequest builds a request to the fixed internal prefix. Only the
// service token and the caller's own Authorization header are attached; no
// client header (X-MA-Actor included) and no arbitrary upstream URL is relayed.
func (s *Server) newAgentRunRequest(ctx context.Context, c *gin.Context, method, path string, body io.Reader) (*http.Request, error) {
	req, err := http.NewRequestWithContext(ctx, method, s.cfg.AgentChatURL+path, body)
	if err != nil {
		return nil, err
	}
	req.Header.Set("X-PowerSystem-Token", s.cfg.AgentToken)
	if auth := c.GetHeader("Authorization"); auth != "" {
		req.Header.Set("Authorization", auth)
	}
	return req, nil
}

func agentRunPath(id, suffix string) string {
	return "/internal/agent/runs/" + id + suffix
}

// parseAgentRunCursor accepts only an ASCII decimal non-negative sequence:
// no sign ("+1"), no whitespace, no empty value, no overflow. An explicitly
// empty after_seq is therefore a client error rather than "no cursor".
func parseAgentRunCursor(value string) (int64, bool) {
	if value == "" || len(value) > 19 {
		return 0, false
	}
	for i := 0; i < len(value); i++ {
		if value[i] < '0' || value[i] > '9' {
			return 0, false
		}
	}
	seq, err := strconv.ParseInt(value, 10, 64)
	if err != nil {
		return 0, false
	}
	return seq, true
}

func agentRunUpstreamStatus(code int) int {
	switch code {
	case http.StatusOK, http.StatusAccepted,
		http.StatusBadRequest, http.StatusUnauthorized, http.StatusForbidden, http.StatusNotFound,
		http.StatusConflict, http.StatusGone, http.StatusUnprocessableEntity, http.StatusTooManyRequests,
		http.StatusServiceUnavailable:
		return code
	}
	return http.StatusServiceUnavailable
}

// proxyAgentRunJSON relays a bounded JSON response without rewriting its status:
// 202 acceptance and 400/404/409/410/422/503 failures keep their meaning.
func (s *Server) proxyAgentRunJSON(c *gin.Context, resp *http.Response) {
	defer resp.Body.Close()
	status := agentRunUpstreamStatus(resp.StatusCode)
	if status != resp.StatusCode {
		fail(c, 503, 50301, "agent runs unavailable")
		return
	}
	limit := int64(agentRunJSONLimit)
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		limit = agentRunErrorLimit
	}
	raw, err := io.ReadAll(io.LimitReader(resp.Body, limit+1))
	if err != nil || int64(len(raw)) > limit {
		fail(c, 503, 50301, "invalid agent runs response")
		return
	}
	if !json.Valid(raw) {
		if resp.StatusCode >= 200 && resp.StatusCode < 300 {
			fail(c, 503, 50301, "invalid agent runs response")
		} else {
			fail(c, status, status*100+1, "agent runs request failed")
		}
		return
	}
	c.Data(status, "application/json; charset=utf-8", raw)
}
