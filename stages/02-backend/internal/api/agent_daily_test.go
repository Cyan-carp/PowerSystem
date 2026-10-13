package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"powersystem/backend/internal/config"
	"strings"
	"testing"
)

func TestAgentProgramReplyProxy(t *testing.T) {
	for _, status := range []string{"answered", "out_of_scope"} {
		t.Run(status, func(t *testing.T) {
			upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				var body map[string]any
				if json.NewDecoder(r.Body).Decode(&body) != nil || body["user_id"] != float64(17) {
					t.Error("program replies must retain authenticated identity")
				}
				w.Header().Set("Content-Type", "application/json")
				json.NewEncoder(w).Encode(map[string]any{"request_id": "r", "session_id": "s", "status": status,
					"answer_markdown": "服务范围提示", "response_mode": "boundary", "model_called": false, "model": "",
					"boundary": map[string]string{"action": "block", "reason": "food", "rule_version": "bnd-m2-v1"}})
			}))
			defer upstream.Close()
			s := &Server{cfg: config.Config{AgentEnabled: true, AgentToken: "private", AgentChatURL: upstream.URL}}
			router := agentRouter(s)
			out := httptest.NewRecorder()
			router.ServeHTTP(out, httptest.NewRequest("POST", "/agent", strings.NewReader(`{"message":"推荐晚饭"}`)))
			var response struct {
				Data map[string]any `json:"data"`
			}
			if out.Code != 200 || json.Unmarshal(out.Body.Bytes(), &response) != nil || response.Data["status"] != status || response.Data["model_called"] != false || response.Data["model"] != "" {
				t.Fatalf("program reply proxy failed: HTTP %d", out.Code)
			}
		})
	}
}
