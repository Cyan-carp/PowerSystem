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
	db, err := gorm.Open(postgres.Open(cfg.PostgresDSN), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		return nil, err
	}
	sqlDB, err := db.DB()
	if err != nil {
		return nil, err
	}
	sqlDB.SetMaxOpenConns(12)
	sqlDB.SetMaxIdleConns(4)
	sqlDB.SetConnMaxLifetime(30 * time.Minute)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err = sqlDB.PingContext(ctx); err != nil {
		return nil, err
	}
	return db, nil
}

func Redis(cfg config.Config) (*redis.Client, error) {
	client := redis.NewClient(&redis.Options{Addr: cfg.RedisAddr, Password: cfg.RedisPassword})
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	if err := client.Ping(ctx).Err(); err != nil {
		return nil, err
	}
	return client, nil
}

func Migrate(db *gorm.DB, path string) error {
	content, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	for _, statement := range strings.Split(string(content), ";") {
		sql := strings.TrimSpace(statement)
		if sql == "" {
			continue
		}
		if err := db.Exec(sql).Error; err != nil {
			return err
		}
	}
	if !db.Migrator().HasTable("telemetry_inbox") {
		return errors.New("migration did not create telemetry_inbox")
	}
	return nil
}

func SeedDemoDevices(db *gorm.DB) error {
	for _, code := range []string{"INV-1001", "INV-1002", "INV-1003"} {
		if err := db.Exec("INSERT INTO devices(device_code,name,dev_type,vendor,station_code,group_name) VALUES(?,?,'inverter','synthetic','ST-01','demo') ON CONFLICT(device_code) DO NOTHING", code, code).Error; err != nil {
			return err
		}
	}
	return nil
}
