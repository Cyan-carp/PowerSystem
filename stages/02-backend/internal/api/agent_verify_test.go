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

func TestVerificationIdentityValidationAndUpstreamErrors(t *testing.T) {
	sid := "11111111-1111-1111-1111-111111111111"
	calls, upstreamStatus := 0, 200
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		var body map[string]any
		json.NewDecoder(r.Body).Decode(&body)
		if body["user_id"] != float64(17) || r.Header.Get("Authorization") != "Bearer current-user" || r.Header.Get("X-PowerSystem-Token") != "service-secret" || r.URL.Path != "/internal/agent/sessions/"+sid+"/verify" {
			t.Error("identity or route incorrect")
		}
		w.WriteHeader(upstreamStatus)
		w.Write([]byte(`{"kind":"verification","turn_id":2,"session_id":"` + sid + `"}`))
	}))
	defer upstream.Close()
	s := &Server{cfg: config.Config{AgentEnabled: true, AgentChatURL: upstream.URL, AgentToken: "service-secret"}, chatGate: allowedChatGate{}}
	r := gin.New()
	r.POST("/sessions/:id/verify", func(c *gin.Context) { c.Set("user_id", int64(17)) }, s.agentRAGVerify)
	body := `{"parent_turn_id":1,"device_id":2,"checks":["latest_telemetry"],"verification_id":"22222222-2222-2222-2222-222222222222"}`
	for _, tc := range []struct {
		body string
		want int
	}{{body, 200}, {strings.Replace(body, `"device_id":2`, `"device_id":-1`, 1), 400}, {strings.Replace(body, "latest_telemetry", "control", 1), 400}, {strings.Replace(body, `"device_id":2`, `"device_id":2,"user_id":999`, 1), 400}} {
		w := httptest.NewRecorder()
		req := httptest.NewRequest("POST", "/sessions/"+sid+"/verify", strings.NewReader(tc.body))
		req.Header.Set("Authorization", "Bearer current-user")
		r.ServeHTTP(w, req)
		if w.Code != tc.want {
			t.Fatalf("got %d want %d: %s", w.Code, tc.want, w.Body.String())
		}
	}
	if calls != 1 {
		t.Fatal("invalid requests reached upstream")
	}
	for _, code := range []int{404, 409, 422, 503} {
		upstreamStatus = code
		w := httptest.NewRecorder()
		req := httptest.NewRequest("POST", "/sessions/"+sid+"/verify", strings.NewReader(body))
		req.Header.Set("Authorization", "Bearer current-user")
		r.ServeHTTP(w, req)
		want := code
		if code == 422 {
			want = 400
		}
		if w.Code != want {
			t.Fatalf("upstream %d -> %d", code, w.Code)
		}
	}
}
