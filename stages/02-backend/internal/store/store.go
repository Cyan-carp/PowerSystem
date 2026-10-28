package store

import (
	"context"
	"errors"
	"os"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"

	"powersystem/backend/internal/config"
)

func Postgres(cfg config.Config) (*gorm.DB, error) {
	return PostgresContext(context.Background(), cfg)
}
func PostgresContext(parent context.Context, cfg config.Config) (*gorm.DB, error) {
	db, err := gorm.Open(postgres.Open(cfg.PostgresDSN), &gorm.Config{DisableAutomaticPing: true, Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		return nil, err
	}
	sqlDB, err := db.DB()
	if err != nil {
		return nil, err
	}
	sqlDB.SetMaxOpenConns(28)
	sqlDB.SetMaxIdleConns(28)
	sqlDB.SetConnMaxLifetime(30 * time.Minute)
	ctx, cancel := context.WithTimeout(parent, 5*time.Second)
	defer cancel()
	if err = sqlDB.PingContext(ctx); err != nil {
		sqlDB.Close()
		return nil, err
	}
	return db, nil
}

func Redis(cfg config.Config) (*redis.Client, error) {
	return RedisContext(context.Background(), cfg)
}
func RedisContext(parent context.Context, cfg config.Config) (*redis.Client, error) {
	client := redis.NewClient(&redis.Options{Addr: cfg.RedisAddr, Password: cfg.RedisPassword, ContextTimeoutEnabled: true})
	ctx, cancel := context.WithTimeout(parent, 3*time.Second)
	defer cancel()
	if err := client.Ping(ctx).Err(); err != nil {
		client.Close()
		return nil, err
	}
	return client, nil
}

// Order matters: install state before the repeatable energy reader migration.
var MigrationPaths = []string{
	"deploy/postgres/001_init.sql", "deploy/postgres/002_predictions.sql",
	"deploy/postgres/003_energy.sql", "deploy/postgres/004_business_notifications.sql",
	"deploy/postgres/005_agent_interpretations.sql", "deploy/postgres/006_alarm_statistics.sql",
	"deploy/postgres/007_alarm_closure.sql", "deploy/postgres/008_alarm_archive.sql",
	"deploy/postgres/009_simulation_alarms.sql", "deploy/postgres/010_interpretation_v2.sql",
	"deploy/postgres/011_alarm_maintenance.sql", "deploy/postgres/013_device_remarks.sql",
	"deploy/postgres/014_telemetry_reliability.sql",
	"deploy/postgres/015_telemetry_scheduler.sql",
	"deploy/postgres/016_telemetry_realtime.sql",
}

func Migrate(db *gorm.DB, path string) error {
	content, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	apply := func(tx *gorm.DB) error {
		for _, statement := range strings.Split(string(content), ";") {
			sql := strings.TrimSpace(statement)
			if sql == "" {
				continue
			}
			if err := tx.Exec(sql).Error; err != nil {
				return err
			}
		}
		if !tx.Migrator().HasTable("telemetry_inbox") {
			return errors.New("migration did not create telemetry_inbox")
		}
		return nil
	}
	if strings.HasSuffix(path, "014_telemetry_reliability.sql") || strings.HasSuffix(path, "015_telemetry_scheduler.sql") || strings.HasSuffix(path, "016_telemetry_realtime.sql") {
		return db.Transaction(apply)
	}
	return apply(db)
}

func SeedDemoDevices(db *gorm.DB) error {
	for _, code := range []string{"INV-1001", "INV-1002", "INV-1003"} {
		if err := db.Exec(`INSERT INTO devices(device_code,name,dev_type,vendor,is_simulated,reference_vendor,reference_model,station_code,group_name)
VALUES(?,?,'inverter','synthetic',TRUE,'Huawei','SUN2000-100KTL-M2','ST-01','demo')
ON CONFLICT(device_code) DO UPDATE SET is_simulated=TRUE,reference_vendor='Huawei',reference_model='SUN2000-100KTL-M2'
WHERE devices.vendor='synthetic' AND devices.deleted_at IS NULL`, code, code).Error; err != nil {
			return err
		}
	}
	return nil
}
