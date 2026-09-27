package main

import (
	"context"
	"log"

	"go.uber.org/zap"
	"powersystem/backend/internal/config"
	"powersystem/backend/internal/ingest"
	"powersystem/backend/internal/store"
	"powersystem/backend/internal/tdengine"
)

func main() {
	cfg, err := config.Load("gateway")
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
	td := tdengine.New(cfg)
	if err = td.Init(context.Background()); err != nil {
		logger.Fatal("tdengine", zap.Error(err))
	}
	gateway, err := ingest.NewGateway(cfg, pg, td, logger)
	if err != nil {
		logger.Fatal("gateway", zap.Error(err))
	}
	defer gateway.Close()
	if err = gateway.Run(context.Background()); err != nil {
		logger.Fatal("gateway_run", zap.Error(err))
	}
}
