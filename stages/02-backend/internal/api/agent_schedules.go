package api

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"powersystem/backend/internal/model"
)

// The original S08 local profile remains isolated. The explicit production
// branch below attests grants through RAG and never grants a browser JWT.
func (s *Server) s08Local() bool {
	db, err := url.Parse(s.cfg.PostgresDSN)
	return err == nil && (db.Scheme == "postgres" || db.Scheme == "postgresql") && db.Hostname() == "127.0.0.1" && db.Port() == "25438" && strings.HasPrefix(db.Path, "/v4_s08_") && strings.HasSuffix(db.Path, "_test") && os.Getenv("V4_S08_LOCAL_ENABLED") == "true" && s.cfg.AgentChatURL == "http://127.0.0.1:28981" && s.cfg.OpsURL == "http://127.0.0.1:28983"
}

// scheduleReadDomain selects exactly one bridge domain. An explicit production
// profile never falls through to the S06/S08/S09 local guards.
func (s *Server) scheduleReadDomain() string {
	if os.Getenv("V4_PRODUCTION_PROFILE") != "" {
		if s.v4Production() {
			return "production"
		}
		return ""
	}
	if os.Getenv("V4_JOINT_PROFILE") != "" {
		if s.s09Local() {
			return "joint"
		}
		return ""
	}
	if s.s08Local() {
		return "local"
	}
	return ""
}

func (s *Server) agentSchedulesProxy(c *gin.Context) {
	path := "/internal/agent/schedules"
	sid := c.Param("id")
	if sid != "" {
		if !agentRunUUID.MatchString(sid) {
			fail(c, 400, 40001, "invalid schedule")
			return
		}
		path += "/" + sid
	}
	if strings.HasSuffix(c.Request.URL.Path, "/pause") {
		path += "/pause"
	}
	if strings.HasSuffix(c.Request.URL.Path, "/cancel") {
		path += "/cancel"
	}
	if strings.HasSuffix(c.Request.URL.Path, "/resume") {
		path += "/resume"
	}
	if strings.HasSuffix(c.Request.URL.Path, "/history") {
		path += "/history"
	}
	if strings.HasSuffix(c.Request.URL.Path, "/rule-preview") {
		path = "/internal/agent/rule-preview"
	}
	raw, err := io.ReadAll(io.LimitReader(c.Request.Body, 65537))
	if err != nil || len(raw) > 65536 {
		fail(c, 400, 40001, "invalid schedule request")
		return
	}
	if len(raw) == 0 {
		raw = []byte("{}")
	}
	var value map[string]json.RawMessage
	if json.Unmarshal(raw, &value) != nil || value == nil {
		fail(c, 400, 40001, "invalid schedule request")
		return
	}
	// Internal service still uses strict schemas; the public gateway never relays identities.
	if _, ok := value["user_id"]; ok {
		fail(c, 400, 40001, "identity is server owned")
		return
	}
	query := url.Values{}
	query.Set("user_id", strconv.FormatInt(c.GetInt64("user_id"), 10))
	if c.Query("offset") != "" {
		query.Set("offset", c.Query("offset"))
	}
	ctx, cancel := context.WithTimeout(c.Request.Context(), agentRunTimeout)
	defer cancel()
	req, err := s.newAgentRunRequest(ctx, c, c.Request.Method, path+"?"+query.Encode(), bytes.NewReader(raw))
	if err != nil {
		fail(c, 503, 50301, "schedules unavailable")
		return
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Idempotency-Key", c.GetHeader("Idempotency-Key"))
	// The signed actor is only used to admit a configuration run, never a trigger.
	req.Header.Set("X-MA-Actor", s.maContext(c, "", string(raw)))
	resp, err := agentRunJSONClient.Do(req)
	if err != nil {
		fail(c, 503, 50301, "schedule request unconfirmed; refresh history")
		return
	}
	s.proxyAgentRunJSON(c, resp)
}

type s08Grant struct {
	UserID             int64  `json:"user_id"`
	ActorRole          string `json:"actor_role"`
	ConfigVersion      int64  `json:"config_version"`
	Config             string `json:"config"`
	ScopeHash          string `json:"scope_hash"`
	GrantID            string `json:"grant_id"`
	GrantScope         string `json:"grant_scope"`
	GrantVersion       int64  `json:"grant_version"`
	DestinationID      string `json:"destination_id"`
	DestinationVersion string `json:"destination_version"`
}

func (s *Server) scheduleRead(c *gin.Context) {
	domain := s.scheduleReadDomain()
	if domain == "" || len(s.cfg.AgentToken) < 32 || !hmac.Equal([]byte(c.GetHeader("X-PowerSystem-Token")), []byte(s.cfg.AgentToken)) {
		fail(c, 403, 40301, "isolated schedule bridge required")
		return
	}
	sid, gid := c.Param("id"), c.Query("grant_id")
	if !agentRunUUID.MatchString(sid) || !agentRunUUID.MatchString(gid) {
		fail(c, 400, 40001, "invalid schedule grant")
		return
	}
	var g s08Grant
	if domain == "production" || domain == "joint" {
		var err error
		g, err = s.scheduleAttestation(c.Request.Context(), sid, gid, c.Query("kind"), c.Query("scheduled_for"))
		if err != nil {
			fail(c, 403, 40301, "schedule grant unavailable")
			return
		}
	} else {
		r := s.db.Raw(`SELECT s.user_id,s.actor_role,s.config_version,s.config::text AS config,s.scope_hash,g.grant_id::text AS grant_id,g.scope_hash AS grant_scope,g.config_version AS grant_version,g.destination_id,g.destination_version
 FROM assistant_schedules s JOIN assistant_schedule_grants g ON g.schedule_id=s.id
 WHERE s.id=? AND g.grant_id=? AND s.status='active' AND g.revoked_at IS NULL AND g.expires_at>now() AND g.user_id=s.user_id AND g.approved_by=s.user_id`, sid, gid).Scan(&g)
		if r.Error != nil || r.RowsAffected != 1 {
			fail(c, 403, 40301, "schedule grant inactive")
			return
		}
	}
	var cfg map[string]any
	if json.Unmarshal([]byte(g.Config), &cfg) != nil {
		fail(c, 403, 40301, "schedule configuration invalid")
		return
	}
	var canonical bytes.Buffer
	e := json.NewEncoder(&canonical)
	e.SetEscapeHTML(false)
	_ = e.Encode(cfg)
	hash := sha256.Sum256(bytes.TrimSpace(canonical.Bytes()))
	if hex.EncodeToString(hash[:]) != g.ScopeHash || g.ScopeHash != g.GrantScope || g.ConfigVersion != g.GrantVersion || g.DestinationID != "platform_client" || g.DestinationVersion != "1" || cfg["destination_id"] != "platform_client" || cfg["destination_version"] != "1" || (cfg["task_type"] != "daily_report" && cfg["task_type"] != "inspection") {
		fail(c, 403, 40301, "schedule configuration drift")
		return
	}
	var user model.User
	if s.db.Select("id", "role").First(&user, g.UserID).Error != nil || user.Role != g.ActorRole || (user.Role != "admin" && user.Role != "operator") {
		fail(c, 403, 40301, "schedule permission revoked")
		return
	}
	c.Set("user_id", g.UserID)
	c.Set("user_role", user.Role)
	if c.Query("kind") == "check" {
		c.JSON(200, gin.H{"user_id": g.UserID, "role": user.Role})
		return
	}
	slot, err := time.Parse(time.RFC3339Nano, c.Query("scheduled_for"))
	if err != nil {
		fail(c, 400, 40001, "invalid slot")
		return
	}
	var count int64
	if domain == "local" && (s.db.Raw("SELECT count(*) FROM assistant_schedule_slots WHERE schedule_id=? AND grant_id=? AND scheduled_for=? AND run_id IS NOT NULL", sid, gid, slot).Scan(&count).Error != nil || count != 1) {
		fail(c, 403, 40301, "unclaimed slot")
		return
	}
	q := url.Values{}
	scope, validScope := cfg["scope"].(string)
	if !validScope || (scope != "all" && scope != "business" && scope != "baseline") {
		fail(c, 403, 40301, "schedule scope invalid")
		return
	}
	q.Set("scope", scope)
	if v, ok := cfg["station_code"].(string); ok {
		q.Set("station_code", v)
	}
	if v, ok := cfg["device_id"].(float64); ok {
		q.Set("device_id", strconv.FormatInt(int64(v), 10))
	}
	kind := c.Query("kind")
	if kind == "energy" && cfg["task_type"] == "daily_report" {
		zone := time.FixedZone("Asia/Shanghai", 8*3600)
		end := slot.In(zone)
		end = time.Date(end.Year(), end.Month(), end.Day(), 0, 0, 0, 0, zone)
		q.Set("start_date", end.AddDate(0, 0, -1).Format("2006-01-02"))
		q.Set("end_date", end.Format("2006-01-02"))
		q.Set("group_by", "station")
		q.Set("format", "csv")
	} else if kind == "alarms" && cfg["task_type"] == "inspection" {
		q.Set("start", slot.Add(-24*time.Hour).Format(time.RFC3339))
		q.Set("end", slot.Format(time.RFC3339))
		q.Set("page", c.DefaultQuery("page", "1"))
		q.Set("page_size", "100")
	} else {
		fail(c, 403, 40301, "schedule read capability denied")
		return
	}
	// No caller-supplied range, endpoint, owner, command, or destination survives.
	// Only the same-database S08 test host writes its local schedule audit; the
	// production bridge attests and audits inside the RAG database instead.
	if domain == "local" {
		if err := s.db.Exec("INSERT INTO assistant_schedule_events(schedule_id,user_id,kind,detail) VALUES(?,?,'backend_read',?::jsonb)", sid, g.UserID, `{"capability":"`+kind+`"}`).Error; err != nil {
			fail(c, 503, 50301, "schedule audit unavailable")
			return
		}
	}
	read := &gin.Context{Request: c.Request.Clone(c.Request.Context()), Writer: c.Writer, Params: c.Params}
	read.Request.URL.RawQuery = q.Encode()
	read.Set("user_id", g.UserID)
	read.Set("user_role", user.Role)
	if kind == "energy" {
		s.energyReport(read)
	} else {
		s.listAlarms(read)
	}
}
