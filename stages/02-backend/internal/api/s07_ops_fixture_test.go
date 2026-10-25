package api

// Isolated JWT gateway over actual routes. No service operations live here.
import (
	"fmt"
	"github.com/gin-gonic/gin"
	"go.uber.org/zap"
	"golang.org/x/crypto/bcrypt"
	"net/http"
	"os"
	"powersystem/backend/internal/config"
	"powersystem/backend/internal/model"
	"powersystem/backend/internal/store"
	"strings"
	"testing"
)

func TestS07OpsGateway(t *testing.T) {
	if os.Getenv("S07_BROWSER_SERVE") != "true" {
		t.Skip("opt-in S07 isolated host")
	}
	dsn := os.Getenv("S07_TEST_DSN")
	if !strings.Contains(dsn, "127.0.0.1:25437/v4_s07_20261024_r1_test") {
		t.Fatal("isolated S07 database required")
	}
	cfg := config.Config{PostgresDSN: dsn, JWTSecret: "s07-local-synthetic-jwt-key-not-production", AgentEnabled: true, AgentChatURL: "http://127.0.0.1:28971", AgentToken: "s07-local-synthetic-rag-token-000000000", OpsURL: "http://127.0.0.1:28973", OpsToken: "s07-local-synthetic-ops-token-000000000", MAActorKey: "s07-local-synthetic-actor-key-000000000"}
	db, err := store.Postgres(cfg)
	if err != nil {
		t.Fatal(err)
	}
	hash, _ := bcrypt.GenerateFromPassword([]byte("S07-local-only-2026!"), bcrypt.MinCost)
	for _, id := range []int64{70001, 70002, 70003, 70004} {
		role := "operator"
		if id == 70001 || id == 70003 {
			role = "admin"
		}
		u := model.User{ID: id, Username: fmt.Sprintf("s07-user-%d", id), Role: role, PasswordHash: string(hash), RealName: "S07合成身份"}
		if err := db.Where("id=?", id).FirstOrCreate(&u).Error; err != nil {
			t.Fatal(err)
		}
	}
	gin.SetMode(gin.ReleaseMode)
	s := New(cfg, db, nil, nil, zap.NewNop())
	s.router.GET("/drill/identity", func(c *gin.Context) {
		c.JSON(200, gin.H{"pid": os.Getpid(), "instance": os.Getenv("S07_INSTANCE"), "synthetic": true})
	})
	server := &http.Server{Addr: "127.0.0.1:28972", Handler: s.router}
	s.router.POST("/drill/shutdown", func(c *gin.Context) { c.JSON(200, gin.H{"stopping": true}); go server.Close() })
	if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		t.Fatal(err)
	}
}
