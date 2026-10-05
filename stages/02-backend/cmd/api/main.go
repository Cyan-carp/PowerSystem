package main

import (
	"bufio"
	"context"
	"flag"
	"log"
	"os"
	"strings"

	"go.uber.org/zap"
	"golang.org/x/crypto/bcrypt"
	"powersystem/backend/internal/api"
	"powersystem/backend/internal/config"
	"powersystem/backend/internal/store"
	"powersystem/backend/internal/tdengine"
	"powersystem/backend/internal/telemetry"
)

func main() {
	migrateOnly := flag.Bool("migrate-only", false, "apply database schema and seed demo devices, then exit")
	createAdmin := flag.String("create-admin", "", "create an admin using a password from stdin (server-local operation)")
	flag.Parse()
	cfg, err := config.Load("api")
	if err != nil {
		log.Fatal(err)
	}
	logConfig := zap.NewProductionConfig()
	logConfig.OutputPaths = []string{"stdout"}
	logger, err := logConfig.Build()
	if err != nil {
		log.Fatal(err)
	}
	defer logger.Sync()
	pg, err := store.Postgres(cfg)
	if err != nil {
		logger.Fatal("postgres", zap.Error(err))
	}
	for _, path := range []string{"deploy/postgres/001_init.sql", "deploy/postgres/002_predictions.sql", "deploy/postgres/003_energy.sql", "deploy/postgres/004_business_notifications.sql", "deploy/postgres/005_agent_interpretations.sql"} {
		if err = store.Migrate(pg, path); err != nil {
			logger.Fatal("migration", zap.Error(err))
		}
	}
	if err = store.SeedDemoDevices(pg); err != nil {
		logger.Fatal("seed_devices", zap.Error(err))
	}
	if *createAdmin != "" {
		password, readErr := bufio.NewReader(os.Stdin).ReadString('\n')
		password = strings.TrimRight(password, "\r\n")
		if readErr != nil || !telemetry.Code.MatchString(*createAdmin) || len(*createAdmin) < 3 || len(password) < 12 || len(password) > 72 {
			logger.Fatal("invalid_admin_credentials")
		}
		hash, hashErr := bcrypt.GenerateFromPassword([]byte(password), bcrypt.DefaultCost)
		if hashErr != nil || pg.Exec("INSERT INTO users(username,password_hash,role,real_name) VALUES(?,?,'admin','Administrator')", *createAdmin, string(hash)).Error != nil {
			logger.Fatal("create_admin_failed")
		}
		logger.Info("admin_created", zap.String("username", *createAdmin))
		return
	}
	redis, err := store.Redis(cfg)
	if err != nil {
		logger.Fatal("redis", zap.Error(err))
	}
	defer redis.Close()
	td := tdengine.New(cfg)
	if err = td.Init(context.Background()); err != nil {
		logger.Fatal("tdengine", zap.Error(err))
	}
	if *migrateOnly {
		logger.Info("migration_complete")
		return
	}
	server := api.New(cfg, pg, redis, td, logger)
	if err = server.Run(context.Background()); err != nil {
		logger.Fatal("api", zap.Error(err))
	}
}
