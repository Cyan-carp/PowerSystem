package api

import (
	"bytes"
	"context"
	"errors"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/gin-gonic/gin"
)

const attachmentUploadLimit = 5 << 20

func (s *Server) agentAttachmentUpload(c *gin.Context) {
	name := strings.TrimSpace(c.Query("name"))
	if name == "" || utf8.RuneCountInString(name) > 180 || strings.ContainsAny(name, "/\\\r\n\x00") {
		fail(c, 400, 40001, "文件名不正确")
		return
	}
	s.agentAttachmentProxy(c, "/internal/agent/attachments", url.Values{"name": {name}}, attachmentUploadLimit, "application/octet-stream")
}

func (s *Server) agentAttachmentDiscard(c *gin.Context) {
	attachmentID := c.Param("id")
	if !agentRunUUID.MatchString(attachmentID) {
		fail(c, 400, 40001, "附件编号不正确")
		return
	}
	s.agentAttachmentProxy(c, "/internal/agent/attachments/"+attachmentID, url.Values{}, 0, "")
}

func (s *Server) agentAttachmentQuery(c *gin.Context) {
	s.agentAttachmentProxy(c, "/internal/agent/attachment-queries", url.Values{}, 32<<10, "application/json")
}

func (s *Server) agentAttachmentProxy(c *gin.Context, path string, query url.Values, limit int64, contentType string) {
	userID := c.GetInt64("user_id")
	if userID <= 0 {
		fail(c, 401, 40101, "当前会话不可用")
		return
	}
	if !s.cfg.AgentEnabled || s.cfg.AgentChatURL == "" || s.cfg.AgentToken == "" {
		fail(c, 503, 50301, "助手附件暂不可用")
		return
	}
	var body []byte
	if limit > 0 {
		var err error
		body, err = io.ReadAll(http.MaxBytesReader(c.Writer, c.Request.Body, limit))
		var oversized *http.MaxBytesError
		if errors.As(err, &oversized) {
			fail(c, 413, 41301, "附件或请求超过大小限制")
			return
		}
		if err != nil || len(body) == 0 {
			fail(c, 400, 40001, "请求内容不可读")
			return
		}
	}
	query.Set("user_id", strconv.FormatInt(userID, 10))
	ctx, cancel := context.WithTimeout(c.Request.Context(), 25*time.Second)
	defer cancel()
	req, err := s.newAgentRunRequest(ctx, c, c.Request.Method, path+"?"+query.Encode(), bytes.NewReader(body))
	if err != nil {
		fail(c, 503, 50301, "助手附件暂不可用")
		return
	}
	if contentType != "" {
		req.Header.Set("Content-Type", contentType)
	}
	response, err := agentRunJSONClient.Do(req)
	if err != nil {
		fail(c, 503, 50301, "附件服务暂不可用，请重试")
		return
	}
	defer response.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(response.Body, (64<<10)+1))
	if err != nil || len(raw) > 64<<10 || response.StatusCode >= 500 || response.StatusCode == http.StatusUnauthorized {
		fail(c, 503, 50301, "附件服务暂不可用，请重试")
		return
	}
	c.Header("Cache-Control", "no-store")
	c.Data(response.StatusCode, "application/json", raw)
}
