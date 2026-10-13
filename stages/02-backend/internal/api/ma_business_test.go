package api

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/redis/go-redis/v9"
	"go.uber.org/zap"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"powersystem/backend/internal/config"
	"powersystem/backend/internal/model"
	"powersystem/backend/internal/store"
	"powersystem/backend/internal/tdengine"
	"powersystem/backend/internal/telemetry"
)

// Real dedicated PostgreSQL/Redis; TDengine, inference and notification delivery
// are loopback fixtures. This never sends an external notification or uses MQTT.
func TestMAC19BusinessContinuesWithOperationUnavailable(t *testing.T) {
	dsn, rawRedis := os.Getenv("NATURAL_TEST_POSTGRES_DSN"), os.Getenv("MA_BUSINESS_TEST_REDIS_URL")
	if dsn == "" || rawRedis == "" {
		t.Skip("explicit independent MA business fixtures required")
	}
	parsed, parseErr := url.Parse(dsn)
	redisOptions, redisErr := redis.ParseURL(rawRedis)
	redisAllowed := parseErr == nil && redisErr == nil && ((parsed.Path == "/ma_m1_business_test" && redisOptions.Addr == "127.0.0.1:16379" && redisOptions.DB == 0) || (parsed.Path == "/ma_m2_business_test" && redisOptions.Addr == "127.0.0.1:16378" && redisOptions.DB == 8) || (parsed.Path == "/ma_m3_business_test" && redisOptions.Addr == "127.0.0.1:16377" && redisOptions.DB == 8))
	if parseErr != nil || parsed.Hostname() != "127.0.0.1" || (parsed.Path != "/ma_m1_business_test" && parsed.Path != "/ma_m2_business_test" && parsed.Path != "/ma_m3_business_test") || !redisAllowed {
		t.Fatal("dedicated MA test destinations required")
	}
	db, err := gorm.Open(postgres.Open(dsn), &gorm.Config{})
	if err != nil {
		t.Fatal("test postgres unavailable")
	}
	sqlDB, _ := db.DB()
	defer sqlDB.Close()
	for _, name := range []string{"001_init.sql", "002_predictions.sql", "003_energy.sql", "004_business_notifications.sql", "005_agent_interpretations.sql", "006_alarm_statistics.sql"} {
		if err = store.Migrate(db, filepath.Join("../../deploy/postgres", name)); err != nil {
			t.Fatal(err)
		}
	}
	tx := db.Begin()
	defer tx.Rollback()
	rd := redis.NewClient(redisOptions)
	defer rd.Close()
	ctx := context.Background()
	if err = rd.Ping(ctx).Err(); err != nil {
		t.Fatal("test redis unavailable")
	}
	code := "MA-C19-" + fmt.Sprint(time.Now().UnixNano())
	device := model.Device{DeviceCode: code, Name: code, DevType: "inverter", StationCode: "MA-C19", GroupName: code}
	if err = tx.Create(&device).Error; err != nil {
		t.Fatal(err)
	}
	defer rd.Del(ctx, fmt.Sprintf("device:%d:latest", device.ID))
	rule := model.AlarmRule{DeviceID: device.ID, Metric: "temperature", Operator: ">", Threshold: 65, Level: "major", Enabled: true}
	if err = tx.Create(&rule).Error; err != nil {
		t.Fatal(err)
	}
	sample := telemetry.Sample{SchemaVersion: 1, RunID: code, DeviceCode: code, StationCode: "MA-C19", Seq: 1, TS: time.Now().UnixMilli(), Voltage: 400, Current: 100, Power: 68.589211, Temperature: 76, Status: 1}
	payload, _ := json.Marshal(sample)
	inbox := model.Inbox{RunID: code, DeviceID: device.ID, Seq: 1, TS: sample.TS, Payload: payload, ReceivedAt: time.Now()}
	if err = tx.Create(&inbox).Error; err != nil {
		t.Fatal(err)
	}
	td := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		rows := [][]any{{1}}
		if strings.Contains(string(raw), "SELECT ts,voltage") {
			rows = nil
			for i := 0; i < 30; i++ {
				rows = append(rows, []any{sample.TS - int64(29-i)*60000, 400, 100, 76, 68.589211})
			}
		}
		json.NewEncoder(w).Encode(map[string]any{"code": 0, "data": rows})
	}))
	defer td.Close()
	inference := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		json.NewEncoder(w).Encode(map[string]any{"device_id": device.ID, "window_end_ms": sample.TS, "probability": .9, "threshold": .8, "model_version": "ma-c19-fixture", "top_factors": []any{}, "source": "synthetic-trained"})
	}))
	defer inference.Close()
	delivered := 0
	notifier := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { delivered++; w.WriteHeader(200) }))
	defer notifier.Close()
	cfg := config.Config{OpsURL: "http://127.0.0.1:1", OpsToken: strings.Repeat("s", 32), MAActorKey: strings.Repeat("k", 32), JWTSecret: strings.Repeat("j", 32), TDURL: td.URL, TDDatabase: "ma_c19_fixture", AIURL: inference.URL, BusinessNotifyURL: notifier.URL + "/business", BusinessNotifyToken: strings.Repeat("b", 32)}
	srv := New(cfg, tx, rd, tdengine.New(cfg), zap.NewNop())
	srv.processPending(ctx)
	var processed int64
	tx.Raw("SELECT count(*) FROM telemetry_inbox WHERE id=? AND processed_at IS NOT NULL", inbox.ID).Scan(&processed)
	if processed != 1 {
		t.Fatal("telemetry was blocked by unavailable Operation")
	}
	if _, err = rd.Get(ctx, fmt.Sprintf("device:%d:latest", device.ID)).Result(); err != nil {
		t.Fatal("latest telemetry not cached")
	}
	if err = srv.predictDevice(ctx, device); err != nil {
		t.Fatal(err)
	}
	var predictions, alarms int64
	tx.Model(&model.PredictionRecord{}).Where("device_id=?", device.ID).Count(&predictions)
	tx.Model(&model.AlarmRecord{}).Where("device_id=?", device.ID).Count(&alarms)
	if predictions != 1 || alarms != 2 {
		t.Fatalf("prediction/alarm propagation %d/%d", predictions, alarms)
	}
	for i := 0; i < 2; i++ {
		if err = srv.deliverBusinessNotification(ctx); err != nil {
			t.Fatal(err)
		}
	}
	if delivered != 2 {
		t.Fatal("business notification delivery blocked")
	}
	user := model.User{Username: code, PasswordHash: "fixture", Role: "operator"}
	if err = tx.Create(&user).Error; err != nil {
		t.Fatal(err)
	}
	token, _ := srv.sign(user)
	paths := []string{"/api/v1/agent/tasks", "/api/v1/devices", "/api/v1/alarms", fmt.Sprintf("/api/v1/devices/%d/telemetry/latest", device.ID), fmt.Sprintf("/api/v1/devices/%d/prediction", device.ID)}
	for i, path := range paths {
		w := httptest.NewRecorder()
		r := httptest.NewRequest("GET", path, nil)
		r.Header.Set("Authorization", "Bearer "+token)
		srv.Handler().ServeHTTP(w, r)
		expected := 200
		if i == 0 {
			expected = 503
		}
		if w.Code != expected {
			t.Fatalf("%s got %d", path, w.Code)
		}
	}
	t.Log("MA-C19: real PostgreSQL + dedicated Redis; telemetry processing, alarms, predictions and 2 local notification deliveries continue with unavailable Operation. TDengine/inference/notification fixtures; MQTT and external Feishu excluded.")
}
