// Dedicated local FR/BND acceptance server. Never starts MQTT, notification or prediction workers.
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"

	"go.uber.org/zap"
	"golang.org/x/crypto/bcrypt"
	"powersystem/backend/internal/api"
	"powersystem/backend/internal/config"
	"powersystem/backend/internal/model"
	"powersystem/backend/internal/store"
	"powersystem/backend/internal/tdengine"
	"powersystem/backend/internal/telemetry"
)

func main() {
	root := os.Getenv("FR_BND_RUN")
	if root == "" {
		log.Fatal("FR_BND_RUN is required")
	}
	var cfg config.Config
	raw, err := os.ReadFile(filepath.Join(root, "private", "api.json"))
	if err != nil {
		log.Fatal("fixture config unavailable")
	}
	if json.Unmarshal(raw, &cfg) != nil || cfg.HTTPAddr != "127.0.0.1:18142" || !strings.Contains(cfg.PostgresDSN, "127.0.0.1:15442/fr_bnd_business_test") || cfg.RedisAddr != "127.0.0.1:16442" || cfg.TDURL != "http://127.0.0.1:16441" || cfg.TDDatabase != "fr_bnd_acceptance_test" {
		log.Fatal("dedicated acceptance destinations required")
	}
	db, err := store.Postgres(cfg)
	if err != nil {
		log.Fatal("fixture postgres unavailable")
	}
	for _, name := range []string{"001_init.sql", "002_predictions.sql", "003_energy.sql", "004_business_notifications.sql", "005_agent_interpretations.sql", "006_alarm_statistics.sql", "007_alarm_closure.sql", "008_alarm_archive.sql"} {
		if store.Migrate(db, filepath.Join("deploy/postgres", name)) != nil {
			log.Fatal("fixture migration failed")
		}
	}
	rd, err := store.Redis(cfg)
	if err != nil {
		log.Fatal("fixture redis unavailable")
	}
	defer rd.Close()
	td := tdengine.New(cfg)
	if td.Init(context.Background()) != nil {
		log.Fatal("fixture TD init failed")
	}
	var count int64
	db.Model(&model.Device{}).Count(&count)
	now := time.Now().UTC()
	start := now.Truncate(time.Minute).Add(-10 * time.Minute)
	end := start.Add(time.Minute)
	var devices []model.Device
	if count == 0 {
		password, err := os.ReadFile(filepath.Join(root, "private", "admin-password"))
		if err != nil {
			log.Fatal("fixture user secret missing")
		}
		hash, _ := bcrypt.GenerateFromPassword(password, bcrypt.DefaultCost)
		if db.Create(&model.User{ID: 91001, Username: "fr_bnd_admin", PasswordHash: string(hash), Role: "admin", RealName: "隔离验收"}).Error != nil {
			log.Fatal("fixture user failed")
		}
		for i, code := range []string{"A-01", "A-02", "A-03", "B-01", "B-02", "B-03", "B-04"} {
			created := start.Add(-24 * time.Hour)
			if i == 2 {
				created = start.Add(30 * time.Second)
			}
			d := model.Device{DeviceCode: code, Name: "隔离验收 " + code, DevType: "inverter", Vendor: "synthetic", IsSimulated: true, ReferenceVendor: "Huawei", ReferenceModel: "SUN2000-100KTL-M2", StationCode: "FR-" + code[:1], CreatedAt: created, UpdatedAt: created}
			if db.Create(&d).Error != nil {
				log.Fatal("fixture device failed")
			}
			devices = append(devices, d)
			if td.EnsureDevice(context.Background(), d) != nil {
				log.Fatal("fixture TD table failed")
			}
			samples := []telemetry.Sample{}
			if i == 0 {
				for j, status := range []int{1, 0, 2} {
					samples = append(samples, telemetry.Sample{TS: start.Add(time.Duration(j*10) * time.Second).UnixMilli(), Seq: int64(j), Voltage: 400.93, Current: 100, Temperature: 35, Power: 68, Status: status})
				}
			} else {
				from := 0
				if i == 2 {
					from = 30
				}
				for j := from; j < 60; j += 15 {
					samples = append(samples, telemetry.Sample{TS: start.Add(time.Duration(j) * time.Second).UnixMilli(), Seq: int64(j), Voltage: 400.93, Current: 100, Temperature: 35, Power: 68, Status: 1})
				}
			}
			if td.Insert(context.Background(), d, samples) != nil {
				log.Fatal("fixture samples failed")
			}
		}
		rule := model.AlarmRule{DeviceID: devices[1].ID, Metric: "voltage", Operator: ">", Threshold: 240, Level: "urgent", Enabled: true, Version: "v1"}
		if db.Create(&rule).Error != nil {
			log.Fatal("fixture rule failed")
		}
		at := func(seconds int) *time.Time { v := start.Add(time.Duration(seconds) * time.Second); return &v }
		reason := func(s string) *string { return &s }
		records := []model.AlarmRecord{
			{DeviceID: devices[1].ID, RuleID: &rule.ID, Metric: "voltage", Level: "urgent", Value: 400.93, Threshold: 240, Status: "acked", RuleOperator: ">", RuleVersion: "v1", TriggeredAt: *at(5), AckedAt: at(70)},
			{DeviceID: devices[3].ID, Metric: "temperature", Level: "major", Status: "recovered", TriggeredAt: *at(10), AckedAt: at(20), RecoveredAt: at(40), CloseReason: reason("telemetry_recovered")},
			{DeviceID: devices[4].ID, Metric: "ai_failure_risk", Level: "minor", Status: "recovered", TriggeredAt: *at(15), RecoveredAt: at(45), CloseReason: reason("prediction_risk_resolved")},
			{DeviceID: devices[0].ID, Metric: "temperature", Level: "minor", Status: "recovered", TriggeredAt: *at(20), AckedAt: at(25), RecoveredAt: at(50), CloseReason: reason("rule_disabled")},
			{DeviceID: devices[0].ID, Metric: "voltage", Level: "minor", Status: "recovered", TriggeredAt: *at(25), RecoveredAt: at(40), CloseReason: reason("unknown")},
			{DeviceID: devices[3].ID, Metric: "temperature", Level: "major", Status: "unhandled", TriggeredAt: now, Value: 70, Threshold: 60, RuleOperator: ">", RuleVersion: "v1"},
			{DeviceID: devices[4].ID, Metric: "temperature", Level: "minor", Status: "unhandled", TriggeredAt: now, Value: 70, Threshold: 60, RuleOperator: ">", RuleVersion: "v1"},
		}
		for _, a := range records {
			if db.Create(&a).Error != nil {
				log.Fatal("fixture alarm failed")
			}
		}
		meta, _ := json.MarshalIndent(map[string]any{"start": start, "end": end, "device_codes": []string{"A-01", "A-02", "A-03", "B-01", "B-02", "B-03", "B-04"}, "stations": []string{"FR-A", "FR-B"}, "scope": "synthetic dedicated acceptance"}, "", "  ")
		if os.WriteFile(filepath.Join(root, "fixture.json"), meta, 0600) != nil {
			log.Fatal("fixture manifest failed")
		}
	} else {
		if db.Find(&devices).Error != nil {
			log.Fatal("fixture device read failed")
		}
	}
	refresh := func() {
		for i, d := range devices {
			if i == 2 {
				continue
			}
			received := time.Now().UTC()
			sampled := received
			if i == 6 {
				received = received.Add(-time.Hour)
				sampled = received
			}
			value := map[string]any{"device_id": d.ID, "device_code": d.DeviceCode, "run_id": "fr-bnd-synthetic", "seq": 1, "ts_ms": sampled.UnixMilli(), "received_at": received, "voltage": 400.93, "current": 100.0, "temperature": 35.0, "power": 68.0, "status": 1, "fault_code": 0}
			payload, _ := json.Marshal(value)
			rd.Set(context.Background(), fmt.Sprintf("device:%d:latest", d.ID), payload, 0)
		}
	}
	refresh()
	go func() {
		ticker := time.NewTicker(5 * time.Second)
		defer ticker.Stop()
		for range ticker.C {
			refresh()
		}
	}()
	server := api.New(cfg, db, rd, td, zap.NewNop())
	log.Fatal(http.ListenAndServe(cfg.HTTPAddr, server.Handler()))
}
