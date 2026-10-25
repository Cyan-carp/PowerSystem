package api

// Opt-in S06 host: product JWT, routes and business transactions on a dedicated
// database migrated by cmd/api's real startup. This never runs in production.
import (
	"encoding/json"
	"fmt"
	"github.com/gin-gonic/gin"
	"go.uber.org/zap"
	"golang.org/x/crypto/bcrypt"
	"net/http"
	"net/http/httptest"
	"os"
	"powersystem/backend/internal/config"
	"powersystem/backend/internal/model"
	"powersystem/backend/internal/store"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestS06ActionsGateway(t *testing.T) {
	if os.Getenv("S06_BROWSER_SERVE") != "true" {
		t.Skip("opt-in dedicated local host")
	}
	dsn := os.Getenv("S06_TEST_DSN")
	if !strings.Contains(dsn, "127.0.0.1:25436/v4_s06_20261024_r1_test") {
		t.Fatal("dedicated S06 DB required")
	}
	cfg := config.Config{PostgresDSN: dsn, JWTSecret: "s06-local-synthetic-jwt-key-not-production", AgentEnabled: true, AgentChatURL: "http://127.0.0.1:28961", AgentToken: "s06-local-synthetic-rag-token-000000000", OpsURL: "http://127.0.0.1:28963", OpsToken: "s06-local-synthetic-ops-token-000000000", MAActorKey: "s06-local-synthetic-actor-key-000000000"}
	db, err := store.Postgres(cfg)
	if err != nil {
		t.Fatal(err)
	}
	hash, _ := bcrypt.GenerateFromPassword([]byte("S06-local-only-2026!"), bcrypt.MinCost)
	for _, id := range []int64{60001, 60002, 60003, 60004} {
		role := "operator"
		if id == 60003 {
			role = "admin"
		}
		user := model.User{ID: id, Username: fmt.Sprintf("s06-user-%d", id), Role: role, PasswordHash: string(hash), RealName: "S06合成测试身份"}
		if err := db.Where("id=?", id).FirstOrCreate(&user).Error; err != nil {
			t.Fatal(err)
		}
	}
	for i := int64(601); i <= 630; i++ {
		device := model.Device{ID: i, DeviceCode: fmt.Sprintf("S06-%d", i), Name: fmt.Sprintf("S06合成设备%d", i), DevType: "inverter", Vendor: "synthetic", IsSimulated: true, StationCode: "ST-S06", GroupName: "s06"}
		if err := db.Where("id=?", i).FirstOrCreate(&device).Error; err != nil {
			t.Fatal(err)
		}
		alarm := model.AlarmRecord{ID: 60000 + i, DeviceID: i, Metric: "temperature", Level: "minor", Value: 71, Threshold: 70, Status: "unhandled", TriggeredAt: time.Now().UTC()}
		if err := db.Where("id=?", alarm.ID).FirstOrCreate(&alarm).Error; err != nil {
			t.Fatal(err)
		}
	}
	if err := db.Exec("INSERT INTO energy_daily(device_id,day,kwh,segments_count,updated_at) VALUES(601,'2026-10-23',12.5,2,now()) ON CONFLICT DO NOTHING").Error; err != nil {
		t.Fatal(err)
	}
	gin.SetMode(gin.ReleaseMode)
	s := New(cfg, db, nil, nil, zap.NewNop())
	s.router.GET("/drill/identity", func(c *gin.Context) {
		c.JSON(200, gin.H{"pid": os.Getpid(), "instance": os.Getenv("S06_INSTANCE"), "synthetic": true})
	})
	var fault atomic.Value
	fault.Store("none")
	var arrived atomic.Bool
	wrapper := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/drill/fault" {
			if r.Method == "POST" {
				var v struct {
					Mode string `json:"mode"`
				}
				_ = json.NewDecoder(r.Body).Decode(&v)
				fault.Store(v.Mode)
				arrived.Store(false)
			}
			_ = json.NewEncoder(w).Encode(map[string]any{"mode": fault.Load(), "arrived": arrived.Load(), "synthetic": true})
			return
		}
		mode := fault.Load().(string)
		if r.URL.Path == "/internal/ma-action/v1/execute" {
			arrived.Store(true)
			if mode == "pause" {
				for i := 0; i < 100 && fault.Load().(string) == "pause"; i++ {
					time.Sleep(50 * time.Millisecond)
				}
			}
			if mode == "fail" {
				w.WriteHeader(409)
				_, _ = w.Write([]byte(`{"detail":"synthetic_pre_execution_failure"}`))
				return
			}
			if mode == "unresolved" {
				w.WriteHeader(503)
				return
			}
			if mode == "drop-result" {
				rec := httptest.NewRecorder()
				s.router.ServeHTTP(rec, r)
				w.WriteHeader(503)
				return
			}
		}
		if strings.HasPrefix(r.URL.Path, "/internal/ma-action/v1/receipts/") && (mode == "drop-result" || mode == "unresolved") {
			w.WriteHeader(503)
			return
		}
		s.router.ServeHTTP(w, r)
	})
	server := &http.Server{Addr: "127.0.0.1:28962", Handler: wrapper}
	s.router.POST("/drill/shutdown", func(c *gin.Context) { c.JSON(200, gin.H{"stopping": true}); go server.Close() })
	if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		t.Fatal(err)
	}
}
