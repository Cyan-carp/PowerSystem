package api

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
)

func newMAUUID() string {
	b := make([]byte, 16)
	if _, err := rand.Read(b); err != nil {
		panic("UUID randomness unavailable")
	}
	b[6] = (b[6] & 15) | 64
	b[8] = (b[8] & 63) | 128
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[:4], b[4:6], b[6:8], b[8:10], b[10:])
}

func (s *Server) maContext(c *gin.Context, session, request string) string {
	if len(s.cfg.MAActorKey) < 32 {
		return ""
	}
	now := time.Now().Unix()
	hash := sha256.Sum256([]byte(request))
	data := map[string]any{"aud": "ma-ops", "kid": "ma-v1", "iat": now, "exp": now + 450, "user_id": c.GetInt64("user_id"), "role": c.GetString("user_role"), "session_id": session, "trace_id": newMAUUID(), "request_hash": hex.EncodeToString(hash[:])}
	var buf bytes.Buffer
	encoder := json.NewEncoder(&buf)
	encoder.SetEscapeHTML(false)
	_ = encoder.Encode(data)
	payload := base64.RawURLEncoding.EncodeToString(bytes.TrimSpace(buf.Bytes()))
	mac := hmac.New(sha256.New, []byte(s.cfg.MAActorKey))
	_, _ = mac.Write([]byte(payload))
	return payload + "." + hex.EncodeToString(mac.Sum(nil))
}

// Called only from the project-internal network; service credential and current JWT are both mandatory.
func (s *Server) maActor(c *gin.Context) {
	if len(s.cfg.OpsToken) < 32 || !hmac.Equal([]byte(c.GetHeader("X-PowerSystem-Token")), []byte(s.cfg.OpsToken)) || len(s.cfg.MAActorKey) < 32 {
		fail(c, 403, 40301, "operation identity unavailable")
		return
	}
	s.authorize(c)
	if c.IsAborted() {
		return
	}
	ok(c, gin.H{"context": s.maContext(c, "", "")})
}

func (s *Server) opsProxy(c *gin.Context) {
	if s.cfg.OpsURL == "" || len(s.cfg.OpsToken) < 32 || len(s.cfg.MAActorKey) < 32 {
		fail(c, 503, 50301, "运维服务不可用")
		return
	}
	path := "/internal/ma/v1/tasks"
	var forwarded []byte
	id := c.Param("id")
	if id != "" {
		if !validSessionID(id) {
			fail(c, 400, 40001, "invalid task id")
			return
		}
		path += "/" + id
		if c.Request.Method == http.MethodPost {
			raw, err := io.ReadAll(io.LimitReader(c.Request.Body, 1025))
			approvalID := c.Param("approval_id")
			if approvalID != "" {
				var decision struct {
					Decision string `json:"decision"`
				}
				decoder := json.NewDecoder(bytes.NewReader(raw))
				decoder.DisallowUnknownFields()
				if err != nil || len(raw) > 1024 || !validSessionID(approvalID) || decoder.Decode(&decision) != nil || decoder.Decode(new(any)) != io.EOF || (decision.Decision != "approve" && decision.Decision != "reject") {
					fail(c, 400, 40001, "invalid approval decision")
					return
				}
				forwarded, _ = json.Marshal(decision)
				path += "/approvals/" + approvalID + "/decision"
			} else {
				if err != nil || len(raw) > 1024 || (strings.TrimSpace(string(raw)) != "" && strings.TrimSpace(string(raw)) != "{}") {
					fail(c, 400, 40001, "invalid cancellation")
					return
				}
				path += "/cancel"
			}
		}
	} else {
		q := url.Values{}
		for k, values := range c.Request.URL.Query() {
			if len(values) != 1 || (k != "session_id" && k != "status" && k != "offset" && k != "limit") {
				fail(c, 400, 40001, "invalid task filter")
				return
			}
			q.Set(k, values[0])
		}
		if v := q.Get("session_id"); v != "" && !validSessionID(v) {
			fail(c, 400, 40001, "invalid session")
			return
		}
		path += "?" + q.Encode()
	}
	ctx, cancel := context.WithTimeout(c.Request.Context(), 15*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, c.Request.Method, s.cfg.OpsURL+path, bytes.NewReader(forwarded))
	if err != nil {
		fail(c, 503, 50301, "运维服务不可用")
		return
	}
	req.Header.Set("X-PowerSystem-Token", s.cfg.OpsToken)
	req.Header.Set("X-MA-Actor", s.maContext(c, c.Query("session_id"), ""))
	req.Header.Set("Authorization", c.GetHeader("Authorization"))
	if len(forwarded) > 0 {
		req.Header.Set("Content-Type", "application/json")
	}
	client := &http.Client{Timeout: 15 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	resp, err := client.Do(req)
	if err != nil {
		fail(c, 503, 50301, "运维服务不可用")
		return
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		status := resp.StatusCode
		if status != 400 && status != 401 && status != 403 && status != 404 && status != 409 && status != 429 {
			status = 503
		}
		if status == 429 {
			retry, err := strconv.Atoi(resp.Header.Get("Retry-After"))
			if err == nil && retry > 0 {
				c.Header("Retry-After", strconv.Itoa(retry))
			}
		}
		fail(c, status, status*100+1, "任务访问失败")
		return
	}
	raw, err := io.ReadAll(io.LimitReader(resp.Body, (8<<20)+1))
	var data any
	if err != nil || len(raw) > 8<<20 || json.Unmarshal(raw, &data) != nil {
		fail(c, 503, 50301, "invalid task response")
		return
	}
	ok(c, data)
}
