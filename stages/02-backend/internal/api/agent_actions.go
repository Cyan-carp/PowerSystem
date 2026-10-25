package api

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"strconv"

	"github.com/gin-gonic/gin"
)

// The public request can only decide an existing action. The owner is injected
// from the database-checked JWT; neither identity nor action arguments are input.
func (s *Server) agentActionDecision(c *gin.Context) {
	rid, aid := c.Param("id"), c.Param("action_id")
	if !agentRunUUID.MatchString(rid) || !agentRunUUID.MatchString(aid) {
		fail(c, 400, 40001, "invalid action")
		return
	}
	var input struct {
		Decision   string `json:"decision"`
		ApprovalID string `json:"approval_id"`
		PlanHash   string `json:"plan_hash"`
	}
	d := json.NewDecoder(http.MaxBytesReader(c.Writer, c.Request.Body, 8192))
	d.DisallowUnknownFields()
	if d.Decode(&input) != nil || d.Decode(new(any)) != io.EOF || (input.Decision != "approve" && input.Decision != "reject") || !agentRunUUID.MatchString(input.ApprovalID) || len(input.PlanHash) != 64 {
		fail(c, 400, 40001, "invalid decision")
		return
	}
	raw, _ := json.Marshal(input)
	ctx, cancel := context.WithTimeout(c.Request.Context(), agentRunTimeout)
	defer cancel()
	req, err := s.newAgentRunRequest(ctx, c, http.MethodPost, agentRunPath(rid, "/actions/"+aid+"/decision?user_id="+strconv.FormatInt(c.GetInt64("user_id"), 10)), bytes.NewReader(raw))
	if err != nil {
		fail(c, 503, 50301, "action authority unavailable")
		return
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := agentRunJSONClient.Do(req)
	if err != nil {
		fail(c, 503, 50301, "action decision unconfirmed; refresh state")
		return
	}
	s.proxyAgentRunJSON(c, resp)
}
