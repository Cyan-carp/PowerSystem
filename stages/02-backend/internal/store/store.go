package store

import (
	"context"
	"errors"
	"os"
	"path/filepath"
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
	"deploy/postgres/017_availability_rollups.sql",
	"deploy/postgres/018_prediction_quality.sql",
	"deploy/postgres/019_prediction_outcome_label_rule.sql",
	"deploy/postgres/020_prediction_quality_daily_label_rule.sql",
	"deploy/postgres/021_prediction_drift_archive.sql",
}

// atomicMigrations 是必须在单个事务里整体生效的迁移。
//
// 这些文件里有多条互相依赖的语句（例如“删主键 + 建主键”），若按语句逐条自动提交，
// 中途失败会让库停在半截状态，而且并发写入可能钻到无主键窗口里插入重复行。
// Migrate 没有版本表、每次启动都重跑，所以这里只登记确实需要原子性的文件，
// 其余保持逐条执行，避免把大表 ALTER 长时间锁在一个事务里。
var atomicMigrations = map[string]bool{
	"014_telemetry_reliability.sql":               true,
	"015_telemetry_scheduler.sql":                 true,
	"016_telemetry_realtime.sql":                  true,
	"017_availability_rollups.sql":                true,
	"019_prediction_outcome_label_rule.sql":       true,
	"020_prediction_quality_daily_label_rule.sql": true,
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
	if atomicMigrations[filepath.Base(path)] {
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
