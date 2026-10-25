package api

import (
	"fmt"
	"github.com/gin-gonic/gin"
	"net/http"
	"net/http/httptest"
	"powersystem/backend/internal/config"
	"testing"
)

func TestArtifactProxyOwnerAndDownload(t *testing.T) {
	for _, status := range []int{200, 401, 410} {
		t.Run(fmt.Sprint(status), func(t *testing.T) {
			upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Query().Get("user_id") != "42" || r.Header.Get("X-PowerSystem-Token") != "local-test-token" {
					t.Error("gateway did not bind identity")
				}
				if status == 200 {
					w.Header().Set("Content-Type", "text/csv; charset=utf-8")
					w.Header().Set("Content-Length", "4")
					w.Header().Set("Content-Disposition", "attachment; filename=report.csv")
					w.Write([]byte("a\n1\n"))
					return
				}
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(status)
				w.Write([]byte(`{"detail":"unavailable"}`))
			}))
			defer upstream.Close()
			s := &Server{cfg: config.Config{AgentEnabled: true, AgentChatURL: upstream.URL, AgentToken: "local-test-token"}}
			r := gin.New()
			r.GET("/api/v1/agent/artifacts/:id/download", func(c *gin.Context) { c.Set("user_id", int64(42)); s.agentArtifactsProxy(c) })
			w := httptest.NewRecorder()
			r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/api/v1/agent/artifacts/11111111-1111-4111-8111-111111111111/download?user_id=99", nil))
			expected := status
			if status == 401 {
				expected = 503
			}
			if w.Code != expected {
				t.Fatalf("HTTP %d want %d: %s", w.Code, expected, w.Body.String())
			}
			if status == 200 && w.Body.String() != "a\n1\n" {
				t.Fatal("file bytes changed")
			}
		})
	}
}

func TestArtifactJSONEvidenceAndRejectHTML(t *testing.T) {
	for _, mime := range []string{"application/json", "text/html"} {
		upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", mime)
			w.Header().Set("Content-Length", "2")
			w.Write([]byte("{}"))
		}))
		s := &Server{cfg: config.Config{AgentEnabled: true, AgentChatURL: upstream.URL, AgentToken: "local-test-token"}}
		r := gin.New()
		r.GET("/api/v1/agent/artifacts/:id/download", func(c *gin.Context) { c.Set("user_id", int64(42)); s.agentArtifactsProxy(c) })
		w := httptest.NewRecorder()
		r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/api/v1/agent/artifacts/11111111-1111-4111-8111-111111111111/download", nil))
		if mime == "application/json" && (w.Code != 200 || w.Body.String() != "{}") || mime == "text/html" && w.Code != 503 {
			t.Fatalf("mime %s returned HTTP %d", mime, w.Code)
		}
		upstream.Close()
	}
}
