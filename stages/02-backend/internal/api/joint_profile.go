package api

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"strings"
	"time"
)

// A distinct test domain. The existing S06/S08 guards remain exact matches.
func (s *Server) s09Local() bool {
	p, err := url.Parse(s.cfg.PostgresDSN)
	if err != nil || os.Getenv("V4_JOINT_PROFILE") != "v4-s09-local" || os.Getenv("V4_S09_LOCAL_ENABLED") != "true" ||
		(p.Scheme != "postgres" && p.Scheme != "postgresql") || p.Hostname() != "127.0.0.1" || p.Port() != "25439" || (p.RawQuery != "" && p.RawQuery != "sslmode=disable&timezone=UTC") || p.Fragment != "" ||
		!regexp.MustCompile(`^/v4_s09_r2_[a-z0-9_]+_business_test$`).MatchString(p.Path) ||
		s.cfg.AgentChatURL != "http://127.0.0.1:28991" || s.cfg.OpsURL != "http://127.0.0.1:28973" || len(s.cfg.MAActorKey) < 32 || len(s.cfg.OpsToken) < 32 {
		return false
	}
	var actual string
	return s.db != nil && s.db.Raw("SELECT current_database()").Scan(&actual).Error == nil && actual == strings.TrimPrefix(p.Path, "/")
}

func (s *Server) scheduleAttestation(ctx context.Context, sid, gid, kind, slot string) (s08Grant, error) {
	var g s08Grant
	key, err := os.ReadFile(os.Getenv("SCHEDULE_BRIDGE_TOKEN_FILE"))
	if err != nil || len(strings.TrimSpace(string(key))) < 32 {
		return g, io.ErrUnexpectedEOF
	}
	q := url.Values{"grant_id": {gid}, "kind": {kind}}
	if slot != "" {
		q.Set("scheduled_for", slot)
	}
	req, err := http.NewRequestWithContext(ctx, "GET", s.cfg.AgentChatURL+"/internal/schedule-bridge/v1/grants/"+sid+"?"+q.Encode(), nil)
	if err != nil {
		return g, err
	}
	req.Header.Set("X-PowerSystem-Token", strings.TrimSpace(string(key)))
	client := &http.Client{Timeout: 3 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	resp, err := client.Do(req)
	if err != nil {
		return g, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return g, io.ErrUnexpectedEOF
	}
	d := json.NewDecoder(io.LimitReader(resp.Body, 16385))
	d.DisallowUnknownFields()
	err = d.Decode(&g)
	if err == nil && d.Decode(new(any)) != io.EOF {
		err = io.ErrUnexpectedEOF
	}
	return g, err
}
