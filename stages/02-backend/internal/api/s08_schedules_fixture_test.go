package api

// Opt-in S08 local host: actual JWT, route handlers, PG and synthetic historical TD transport.
import (
	"encoding/json"
	"fmt"
	"github.com/gin-gonic/gin"
	"go.uber.org/zap"
	"golang.org/x/crypto/bcrypt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"powersystem/backend/internal/config"
	"powersystem/backend/internal/model"
	"powersystem/backend/internal/store"
	"powersystem/backend/internal/tdengine"
	"strings"
	"testing"
	"time"
)

func TestS08SchedulesGateway(t *testing.T) {
	if os.Getenv("S08_BROWSER_SERVE") != "true" {
		t.Skip("opt-in S08 isolated host")
	}
	dsn := os.Getenv("S08_TEST_DSN")
	if !strings.Contains(dsn, "127.0.0.1:25438/v4_s08_20261024_r1_test") {
		t.Fatal("dedicated S08 DB required")
	}
	cfg := config.Config{PostgresDSN: dsn, JWTSecret: "s08-local-synthetic-jwt-key-not-production", AgentEnabled: true, AgentChatURL: "http://127.0.0.1:28981", AgentToken: "s08-local-synthetic-rag-token-000000000", OpsURL: "http://127.0.0.1:28983", OpsToken: "s08-local-synthetic-ops-token-000000000", MAActorKey: "s08-local-synthetic-actor-key-000000000"}
	db, err := store.Postgres(cfg)
	if err != nil {
		t.Fatal(err)
	}
	hash, _ := bcrypt.GenerateFromPassword([]byte("S08-local-only-2026!"), bcrypt.MinCost)
	for _, id := range []int64{80001, 80002, 80003} {
		role := "operator"
		if id == 80003 {
			role = "admin"
		}
		u := model.User{ID: id, Username: fmt.Sprintf("s08-user-%d", id), Role: role, PasswordHash: string(hash), RealName: "S08合成身份"}
		if err := db.Where("id=?", id).FirstOrCreate(&u).Error; err != nil {
			t.Fatal(err)
		}
	}
	for _, d := range []model.Device{{ID: 801, DeviceCode: "S08-801", StationCode: "PV-01", Name: "S08合成设备一", IsSimulated: true, CreatedAt: time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC)}, {ID: 802, DeviceCode: "S08-802", StationCode: "PV-01", Name: "S08缺测设备", IsSimulated: true, CreatedAt: time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC)}} {
		if err := db.Where("id=?", d.ID).FirstOrCreate(&d).Error; err != nil {
			t.Fatal(err)
		}
	}
	if err := db.Exec("INSERT INTO energy_daily(device_id,day,kwh,segments_count,updated_at) VALUES(801,'2026-10-23',12.5,2,now()),(801,'2026-10-24',13.5,2,now()) ON CONFLICT DO NOTHING").Error; err != nil {
		t.Fatal(err)
	}
	alarm := model.AlarmRecord{ID: 80801, DeviceID: 801, Metric: "temperature", Level: "major", Value: 80, Threshold: 75, Status: "unhandled", TriggeredAt: time.Now().Add(-time.Hour)}
	if err := db.Where("id=?", alarm.ID).FirstOrCreate(&alarm).Error; err != nil {
		t.Fatal(err)
	}
	td := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		q := string(raw)
		rows := [][]any{}
		if strings.HasPrefix(q, "SELECT ts,temperature") {
			start := time.Date(2026, 10, 22, 16, 0, 0, 0, time.UTC)
			for i, v := range []any{60, 72, 80, nil, 74, 78} {
				rows = append(rows, []any{start.Add(time.Duration(i*5) * time.Second).UnixMilli(), v})
			}
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"code": 0, "data": rows})
	}))
	t.Cleanup(td.Close)
	s := New(cfg, db, nil, tdengine.New(config.Config{TDURL: td.URL, TDDatabase: "s08_synthetic"}), zap.NewNop())
	s.fleetCatalog = testFleetCatalog(t)
	s.router.GET("/drill/identity", func(c *gin.Context) {
		c.JSON(200, gin.H{"pid": os.Getpid(), "instance": os.Getenv("S08_INSTANCE"), "synthetic": true, "historical_source": "explicit synthetic TD transport, not real TD runtime"})
	})
	server := &http.Server{Addr: "127.0.0.1:28982", Handler: s.router}
	s.router.POST("/drill/shutdown", func(c *gin.Context) { c.JSON(200, gin.H{"stopping": true}); go server.Close() })
	if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		t.Fatal(err)
	}
}
