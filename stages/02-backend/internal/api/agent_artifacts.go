package api

import (
	"context"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"go.uber.org/zap"
)

// Fixed S05 routes. authorize has already rechecked the current DB account.
// Client user_id, paths, filenames and headers never select storage or owner.
func (s *Server) agentArtifactsProxy(c *gin.Context) {
	if !s.cfg.AgentEnabled || s.cfg.AgentChatURL == "" || s.cfg.AgentToken == "" {
		fail(c, 503, 50301, "artifacts unavailable")
		return
	}
	user := c.GetInt64("user_id")
	if user < 1 {
		fail(c, 401, 40101, "missing identity")
		return
	}
	id := c.Param("id")
	if !agentRunUUID.MatchString(id) {
		if s.log != nil {
			s.log.Warn("artifact_download_rejected", zap.Int64("user_id", user), zap.String("reason", "invalid_artifact_id"))
		}
		fail(c, 400, 40001, "invalid artifact")
		return
	}
	download := strings.HasSuffix(c.Request.URL.Path, "/download")
	path := "/internal/agent/artifacts/" + id
	if download {
		path += "/download"
	}
	path += "?user_id=" + strconv.FormatInt(user, 10)
	ctx, cancel := context.WithTimeout(c.Request.Context(), 60*time.Second)
	defer cancel()
	req, err := s.newAgentRunRequest(ctx, c, http.MethodGet, path, nil)
	if err != nil {
		fail(c, 503, 50301, "artifacts unavailable")
		return
	}
	// A separate timeout allows actual bounded file streaming.
	client := &http.Client{Timeout: 60 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	resp, err := client.Do(req)
	if err != nil {
		fail(c, 503, 50301, "artifacts unavailable")
		return
	}
	if resp.StatusCode == http.StatusUnauthorized {
		resp.Body.Close()
		fail(c, 503, 50301, "artifact service authentication unavailable")
		return
	}
	if !download || resp.StatusCode != http.StatusOK {
		s.proxyAgentRunJSON(c, resp)
		return
	}
	defer resp.Body.Close()
	mime := resp.Header.Get("Content-Type")
	if !(strings.HasPrefix(mime, "text/csv") || mime == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" || mime == "application/json") || resp.ContentLength < 0 || resp.ContentLength > 32<<20 {
		fail(c, 503, 50301, "invalid artifact response")
		return
	}
	c.Header("Content-Type", mime)
	c.Header("Content-Length", strconv.FormatInt(resp.ContentLength, 10))
	c.Header("Content-Disposition", resp.Header.Get("Content-Disposition"))
	c.Header("X-Content-SHA256", resp.Header.Get("X-Content-SHA256"))
	c.Header("Cache-Control", "no-store")
	c.Header("X-Content-Type-Options", "nosniff")
	c.Status(http.StatusOK)
	copied, copyErr := io.Copy(c.Writer, io.LimitReader(resp.Body, resp.ContentLength))
	if (copyErr != nil || copied != resp.ContentLength) && s.log != nil {
		s.log.Warn("artifact_stream_incomplete", zap.Int64("user_id", user), zap.String("artifact_id", id), zap.Int64("bytes", copied))
	}
}
