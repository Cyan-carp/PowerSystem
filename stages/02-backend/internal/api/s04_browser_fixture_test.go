package api

// Opt-in acceptance host: exposes the actual JWT middleware and run/history
// proxies over TCP against an exclusively named, synthetic S04 database.
import (
	"fmt"
	"github.com/gin-gonic/gin"
	"github.com/golang-jwt/jwt/v5"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"net/http"
	"os"
	"powersystem/backend/internal/config"
	"powersystem/backend/internal/model"
	"strings"
	"testing"
	"time"
)

func TestS04BrowserGateway(t *testing.T) {
	if os.Getenv("S04_BROWSER_SERVE") != "true" {
		return
	}
	dsn := os.Getenv("S04_TEST_DSN")
	if !strings.Contains(dsn, "127.0.0.1:25434/v4_s04_20261023_r1_test") {
		t.Fatal("dedicated S04 database required")
	}
	db, err := gorm.Open(postgres.Open(dsn), &gorm.Config{})
	if err != nil {
		t.Fatal(err)
	}
	if err = db.AutoMigrate(&model.User{}); err != nil {
		t.Fatal(err)
	}
	for _, id := range []int64{40001, 40002} {
		user := model.User{ID: id, Username: fmt.Sprintf("s04-user-%d", id), Role: "operator", RealName: "S04合成用户"}
		db.Where("id = ?", id).FirstOrCreate(&user)
	}
	const key = "s04-local-synthetic-jwt-key-not-production"
	s := &Server{db: db, cfg: config.Config{JWTSecret: key, AgentEnabled: true, AgentChatURL: "http://127.0.0.1:28941", AgentToken: "s04-local-synthetic-service-token-00000000"}}
	gin.SetMode(gin.ReleaseMode)
	router := gin.New()
	router.Use(gin.Recovery())
	router.GET("/drill/identity", func(c *gin.Context) {
		c.JSON(200, gin.H{"pid": os.Getpid(), "instance": os.Getenv("S04_INSTANCE"), "synthetic": true})
	})
	router.POST("/api/v1/auth/login", func(c *gin.Context) {
		var body struct {
			Username string `json:"username"`
		}
		if c.ShouldBindJSON(&body) != nil {
			c.Status(400)
			return
		}
		id := int64(40001)
		if body.Username == "s04-other" {
			id = 40002
		}
		signed, _ := jwt.NewWithClaims(jwt.SigningMethodHS256, jwt.MapClaims{"sub": fmt.Sprint(id), "exp": time.Now().Add(6 * time.Hour).Unix()}).SignedString([]byte(key))
		ok(c, gin.H{"token": signed, "token_type": "Bearer", "expires_in": 21600, "user": gin.H{"id": id, "username": body.Username, "real_name": "S04合成用户", "role": "operator"}})
	})
	a := router.Group("/api/v1", s.authorize)
	a.POST("/agent/runs", s.agentRunsProxy)
	a.GET("/agent/runs/:id", s.agentRunsProxy)
	a.GET("/agent/runs/:id/events", s.agentRunsProxy)
	a.POST("/agent/runs/:id/cancel", s.agentRunsProxy)
	a.GET("/agent/sessions", s.agentRAGProxy)
	a.GET("/agent/sessions/:id", s.agentRAGProxy)
	a.DELETE("/agent/sessions/:id", s.agentRAGProxy)
	a.GET("/agent/rag/sources/:id", s.agentRAGProxy)
	a.GET("/agent/tasks", func(c *gin.Context) { ok(c, gin.H{"list": []any{}, "offset": 0, "page_size": 50}) })
	a.GET("/agent/model-status", func(c *gin.Context) {
		ok(c, gin.H{"available": true, "model": "deepseek-flash", "status": "synthetic acceptance"})
	})
	if err := http.ListenAndServe("127.0.0.1:28942", router); err != nil {
		t.Fatal(err)
	}
}
