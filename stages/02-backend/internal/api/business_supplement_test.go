package api

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"

	"powersystem/backend/internal/config"
	"powersystem/backend/internal/model"
)

func TestSupplementAttachmentGatewayUsesSessionIdentity(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/internal/agent/attachments" || r.URL.Query().Get("user_id") != "42" || r.URL.Query().Get("name") != "巡检.log" || r.Header.Get("X-PowerSystem-Token") != "fixture" {
			t.Error("gateway forwarded the wrong route or identity")
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"id":"attachment-fixture"}`))
	}))
	defer upstream.Close()
	s := &Server{cfg: config.Config{AgentEnabled: true, AgentChatURL: upstream.URL, AgentToken: "fixture"}}
	r := gin.New()
	r.POST("/agent/attachments", func(c *gin.Context) { c.Set("user_id", int64(42)) }, s.agentAttachmentUpload)
	path := "/agent/attachments?name=" + url.QueryEscape("巡检.log") + "&user_id=999"
	for _, tc := range []struct {
		size int
		want int
	}{{10, http.StatusOK}, {attachmentUploadLimit + 1, http.StatusRequestEntityTooLarge}} {
		request := httptest.NewRequest(http.MethodPost, path, bytes.NewReader(make([]byte, tc.size)))
		result := httptest.NewRecorder()
		r.ServeHTTP(result, request)
		if result.Code != tc.want {
			t.Fatalf("size %d: got %d, want %d", tc.size, result.Code, tc.want)
		}
	}
}

func TestSupplementLinkStates(t *testing.T) {
	now := time.Now().UTC()
	old, current, future := now.Add(-time.Minute), now.Add(-time.Second), now.Add(time.Second)
	for _, tc := range []struct {
		name     string
		receipt  *time.Time
		accepted *time.Time
		peer     bool
		platform bool
		health   string
		want     string
	}{
		{"current", &current, &current, true, true, "online", "current"},
		{"inbox ahead of TD", &current, &old, true, true, "offline", "processing_delay"},
		{"first receipt", &current, nil, true, true, "unknown", "unknown"},
		{"isolated disconnect", &old, &old, true, true, "offline", "offline"},
		{"one station not reporting", &old, &old, false, true, "offline", "no_new_reports"},
		{"all sites quiet", &old, &old, false, false, "offline", "unknown"},
		{"future receipt", &future, &old, true, true, "offline", "unknown"},
		{"no observation", nil, nil, true, true, "offline", "unknown"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			row := telemetryLinkRow{ReceivedAt: tc.receipt, ConfirmedReceived: tc.accepted}
			if got := classifyLink(now, row, tc.peer, tc.platform, tc.health); got != tc.want {
				t.Fatalf("got %q, want %q", got, tc.want)
			}
		})
	}
}

func TestSupplementPostgres(t *testing.T) {
	dsn := os.Getenv("BUSINESS_SUPPLEMENT_TEST_DSN")
	if dsn == "" {
		t.Skip("requires isolated PostgreSQL fixture")
	}
	endpoint, err := url.Parse(dsn)
	if err != nil || endpoint.Hostname() != "127.0.0.1" || endpoint.Port() != "29873" || endpoint.Path != "/postgres" {
		t.Fatal("fixture must use the dedicated local test socket")
	}
	db, err := gorm.Open(postgres.New(postgres.Config{DSN: dsn, PreferSimpleProtocol: true}), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		t.Fatal(err)
	}
	pool, err := db.DB()
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	pool.SetMaxOpenConns(1)
	var isolated bool
	if err := db.Raw(`SELECT EXISTS (SELECT 1 FROM supplement_fixture WHERE marker='isolated')`).Scan(&isolated).Error; err != nil || !isolated {
		t.Fatal("fixture marker missing", err)
	}
	gin.SetMode(gin.TestMode)
	s := &Server{cfg: config.Config{JWTSecret: strings.Repeat("x", 32)}, db: db}
	r := gin.New()
	a := r.Group("/api/v1", s.authorize)
	a.POST("/alarms/:id/field-records", s.alarmFieldRecords)
	a.GET("/devices/:id/field-records", s.deviceFieldRecords)
	a.POST("/predictions/:id/feedback", s.predictionFeedback)
	a.GET("/predictions/:id/feedback", s.predictionFeedback)
	a.GET("/predictions/quality/human-review", s.humanReview)
	a.GET("/devices/:id/telemetry/link-status", s.deviceLinkStatus)
	a.GET("/dashboard/telemetry-link", s.platformLinkStatus)
	token, err := s.sign(model.User{ID: 1, Role: "operator"})
	if err != nil {
		t.Fatal(err)
	}
	call := func(method, path, bearer string, body any, want int) string {
		t.Helper()
		raw, _ := json.Marshal(body)
		req := httptest.NewRequest(method, "/api/v1"+path, bytes.NewReader(raw))
		req.Header.Set("Content-Type", "application/json")
		if bearer != "" {
			req.Header.Set("Authorization", "Bearer "+bearer)
		}
		result := httptest.NewRecorder()
		r.ServeHTTP(result, req)
		if result.Code != want {
			t.Fatalf("%s %s got %d, want %d: %s", method, path, result.Code, want, result.Body)
		}
		return result.Body.String()
	}
	call(http.MethodGet, "/devices/1/field-records", "", nil, 401)
	record := map[string]any{"request_id": "10000000-0000-4000-8000-000000000001", "symptoms": "设备异常", "measures": "现场检查", "result": "完成检查", "actor_id": 999}
	first := call(http.MethodPost, "/alarms/1/field-records", token, record, 200)
	if first != call(http.MethodPost, "/alarms/1/field-records", token, record, 200) {
		t.Fatal("retry did not return the same field record")
	}
	listResult := call(http.MethodGet, "/devices/1/field-records", token, nil, 200)
	if !strings.Contains(listResult, `"actor_name":"fixture-operator"`) || !strings.Contains(listResult, `"alarm_id":1`) {
		t.Fatal("alarm record not visible with server actor in device history", listResult)
	}
	feedback := map[string]any{"request_id": "10000000-0000-4000-8000-000000000004", "status": "not_occurred", "note": "持续观察"}
	call(http.MethodPost, "/predictions/2/feedback", token, feedback, 400)
	feedback["status"], feedback["request_id"] = "watching", "10000000-0000-4000-8000-000000000002"
	response := call(http.MethodPost, "/predictions/1/feedback", token, feedback, 200)
	var stored struct{ Data feedbackRow }
	if err := json.Unmarshal([]byte(response), &stored); err != nil || stored.Data.ID == 0 {
		t.Fatal("missing feedback id", err, response)
	}
	feedback["status"], feedback["request_id"] = "not_occurred", "10000000-0000-4000-8000-000000000003"
	stale := map[string]any{"request_id": "10000000-0000-4000-8000-000000000005", "status": "not_occurred", "note": "过期版本"}
	call(http.MethodPost, "/predictions/1/feedback", token, stale, 409)
	feedback["previous_id"] = stored.Data.ID
	call(http.MethodPost, "/predictions/1/feedback", token, feedback, 200)
	stats := call(http.MethodGet, "/predictions/quality/human-review", token, nil, 200)
	if !strings.Contains(stats, `"not_occurred":1`) || !strings.Contains(stats, `"watching":0`) || !strings.Contains(stats, `"device_code":"FIX-01"`) {
		t.Fatal("summary must count the latest revision per prediction", stats)
	}
	var raw model.PredictionRecord
	if err := db.First(&raw, 1).Error; err != nil || raw.Probability != .8 || raw.Threshold != .5 || raw.ModelVersion != "fixture" {
		t.Fatal("feedback changed the prediction evidence", err, raw)
	}
	link := call(http.MethodGet, "/devices/1/telemetry/link-status", token, nil, 200)
	if !strings.Contains(link, `"coverage":[`) || !strings.Contains(link, `"gap_count":2`) {
		t.Fatal("verified coverage was not projected", link)
	}
	platform := call(http.MethodGet, "/dashboard/telemetry-link", token, nil, 200)
	if !strings.Contains(platform, `"known":false`) || !strings.Contains(platform, `"pending_in_window":null`) {
		t.Fatal("missing queue probe must not be reported as zero", platform)
	}
}
