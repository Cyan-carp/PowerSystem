package api

// Opt-in loopback acceptance host. Real PostgreSQL + JWT + production report
// handlers; TD transport supplies registered synthetic samples, not CSV files.
import (
	"encoding/json"
	"fmt"
	"github.com/gin-gonic/gin"
	"github.com/golang-jwt/jwt/v5"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"powersystem/backend/internal/config"
	"powersystem/backend/internal/model"
	"powersystem/backend/internal/tdengine"
	"regexp"
	"strconv"
	"strings"
	"testing"
	"time"
)

func TestS05DeliveryGateway(t *testing.T) {
	if os.Getenv("S05_BROWSER_SERVE") != "true" {
		return
	}
	dsn := os.Getenv("S05_TEST_DSN")
	if !strings.Contains(dsn, "127.0.0.1:25435/v4_s05_20261023_r1_test") {
		t.Fatal("dedicated S05 DB required")
	}
	db, err := gorm.Open(postgres.Open(dsn), &gorm.Config{})
	if err != nil {
		t.Fatal(err)
	}
	if err = db.AutoMigrate(&model.User{}, &model.Device{}, &model.AlarmRecord{}); err != nil {
		t.Fatal(err)
	}
	for _, id := range []int64{50001, 50002} {
		user := model.User{ID: id, Username: fmt.Sprintf("s05-user-%d", id), Role: "operator", RealName: "S05合成用户"}
		if err = db.Where("id = ?", id).FirstOrCreate(&user).Error; err != nil {
			t.Fatal(err)
		}
	}
	start := time.Date(2026, 10, 20, 0, 0, 0, 0, time.UTC)
	for _, d := range []model.Device{{ID: 501, DeviceCode: "S05-501", StationCode: "PV-01", Name: "S05合成设备一", CreatedAt: start.AddDate(0, 0, -30)}, {ID: 502, DeviceCode: "S05-502", StationCode: "PV-01", Name: "S05合成设备二", CreatedAt: start.AddDate(0, 0, -30)}, {ID: 503, DeviceCode: "S05-503", StationCode: "PV-02", Name: "S05合成设备三", CreatedAt: start.AddDate(0, 0, -30)}} {
		if err = db.Where("id = ?", d.ID).FirstOrCreate(&d).Error; err != nil {
			t.Fatal(err)
		}
	}
	if err = db.Exec(`CREATE TABLE IF NOT EXISTS energy_daily(device_id bigint, day date, kwh double precision, segments_count bigint, updated_at timestamptz, PRIMARY KEY(device_id,day))`).Error; err != nil {
		t.Fatal(err)
	}
	if err = db.Exec(`INSERT INTO energy_daily VALUES (501,'2026-10-20',10.5,2,'2026-10-22'),(502,'2026-10-20',0,2,'2026-10-22'),(501,'2026-10-21',12,2,'2026-10-22'),(503,'2026-10-20',99,0,'2026-10-22') ON CONFLICT DO NOTHING`).Error; err != nil {
		t.Fatal(err)
	}
	ack := start.Add(time.Hour)
	for _, alarm := range []model.AlarmRecord{{ID: 50501, DeviceID: 501, Metric: "temperature", Level: "major", Value: 80, Threshold: 70, Status: "acked", TriggeredAt: start.Add(time.Minute), AckedAt: &ack}, {ID: 50502, DeviceID: 502, Metric: "power", Level: "minor", Value: 1, Threshold: 2, Status: "unhandled", TriggeredAt: start.Add(2 * time.Minute)}} {
		if err = db.Where("id = ?", alarm.ID).FirstOrCreate(&alarm).Error; err != nil {
			t.Fatal(err)
		}
	}
	const key = "s05-local-synthetic-jwt-key-not-production"
	rows := [][]any{{start.Add(time.Second).UnixMilli(), 1, 400, 100, 30, 68, 1, 0}, {start.Add(11 * time.Second).UnixMilli(), 2, 400, 100, 31, 67, 2, 0}}
	td := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		query := string(raw)
		data := [][]any{}
		if strings.HasPrefix(query, "SELECT") {
			selected := [][]any{}
			for _, row := range rows {
				ts := row[0].(int64)
				match := true
				for _, part := range regexp.MustCompile(`ts (>=|<=|>|<) (\d+)`).FindAllStringSubmatch(query, -1) {
					limit, _ := strconv.ParseInt(part[2], 10, 64)
					switch part[1] {
					case ">=":
						match = match && ts >= limit
					case "<=":
						match = match && ts <= limit
					case ">":
						match = match && ts > limit
					case "<":
						match = match && ts < limit
					}
				}
				if match {
					selected = append(selected, row)
				}
			}
			switch {
			case strings.HasPrefix(query, "SELECT COUNT"):
				data = [][]any{{len(selected)}}
			case strings.HasPrefix(query, "SELECT ts,status"):
				if strings.Contains(query, "ORDER BY ts DESC") && len(selected) > 0 {
					selected = selected[len(selected)-1:]
				}
				for _, row := range selected {
					data = append(data, []any{row[0], row[6]})
				}
			default:
				data = selected
			}
		}
		json.NewEncoder(w).Encode(map[string]any{"code": 0, "data": data})
	}))
	t.Cleanup(td.Close)
	s := &Server{db: db, td: tdengine.New(config.Config{TDURL: td.URL, TDDatabase: "s05_synthetic"}), fleetCatalog: testFleetCatalog(t), cfg: config.Config{JWTSecret: key, AgentEnabled: true, AgentChatURL: "http://127.0.0.1:28951", AgentToken: "s05-local-synthetic-service-token-00000000"}}
	gin.SetMode(gin.ReleaseMode)
	router := gin.New()
	router.Use(gin.Recovery())
	router.GET("/drill/identity", func(c *gin.Context) {
		c.JSON(200, gin.H{"pid": os.Getpid(), "instance": os.Getenv("S05_INSTANCE"), "synthetic": true})
	})
	router.POST("/api/v1/auth/login", func(c *gin.Context) {
		var body struct {
			Username string `json:"username"`
		}
		if c.ShouldBindJSON(&body) != nil {
			c.Status(400)
			return
		}
		id := int64(50001)
		if body.Username == "s05-other" {
			id = 50002
		}
		signed, _ := jwt.NewWithClaims(jwt.SigningMethodHS256, jwt.MapClaims{"sub": fmt.Sprint(id), "exp": time.Now().Add(4 * time.Hour).Unix()}).SignedString([]byte(key))
		ok(c, gin.H{"token": signed, "token_type": "Bearer", "expires_in": 14400, "user": gin.H{"id": id, "username": body.Username, "real_name": "S05合成用户", "role": "operator"}})
	})
	a := router.Group("/api/v1", s.authorize)
	a.POST("/agent/runs", s.agentRunsProxy)
	a.GET("/agent/runs/:id", s.agentRunsProxy)
	a.GET("/agent/runs/:id/events", s.agentRunsProxy)
	a.POST("/agent/runs/:id/cancel", s.agentRunsProxy)
	a.GET("/agent/artifacts/:id", s.agentArtifactsProxy)
	a.GET("/agent/artifacts/:id/download", s.agentArtifactsProxy)
	a.GET("/agent/sessions", s.agentRAGProxy)
	a.GET("/agent/sessions/:id", s.agentRAGProxy)
	a.GET("/reports/availability", s.availability)
	a.GET("/reports/alarm-timeliness", s.alarmTimeliness)
	a.GET("/reports/energy", s.energyReport)
	a.GET("/devices/:id/telemetry/export", s.exportTelemetry)
	a.GET("/alarms", s.listAlarms)
	a.GET("/agent/tasks", func(c *gin.Context) { ok(c, gin.H{"list": []any{}, "offset": 0, "page_size": 50}) })
	a.GET("/agent/model-status", func(c *gin.Context) {
		ok(c, gin.H{"available": true, "model": "deepseek-flash", "status": "synthetic acceptance"})
	})
	a.GET("/drill/current-user", func(c *gin.Context) { ok(c, gin.H{"id": c.GetInt64("user_id")}) })
	if err = http.ListenAndServe("127.0.0.1:28952", router); err != nil {
		t.Fatal(err)
	}
}
