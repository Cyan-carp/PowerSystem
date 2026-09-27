package main

import (
	"context"
	"flag"
	"log"

	"go.uber.org/zap"
	"powersystem/backend/internal/api"
	"powersystem/backend/internal/config"
	"powersystem/backend/internal/store"
	"powersystem/backend/internal/tdengine"
)

func main() {
	migrateOnly := flag.Bool("migrate-only", false, "apply database schema and seed demo devices, then exit")
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
	if err = store.Migrate(pg, "deploy/postgres/001_init.sql"); err != nil {
		logger.Fatal("migration", zap.Error(err))
	}
	if err = store.SeedDemoDevices(pg); err != nil {
		logger.Fatal("seed_devices", zap.Error(err))
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
