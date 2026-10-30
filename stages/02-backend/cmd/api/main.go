package main

import (
	"bufio"
	"context"
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"go.uber.org/zap"
	"golang.org/x/crypto/bcrypt"
	"powersystem/backend/internal/api"
	"powersystem/backend/internal/config"
	"powersystem/backend/internal/fleet"
	"powersystem/backend/internal/service"
	"powersystem/backend/internal/store"
	"powersystem/backend/internal/tdengine"
	"powersystem/backend/internal/telemetry"
)

func main() {
	migrateOnly := flag.Bool("migrate-only", false, "apply database schema and seed demo devices, then exit")
	migrationTimeout := flag.Duration("migration-timeout", 30*time.Second, "offline migrate-only budget (30s to 15m); never changes server startup budget")
	createAdmin := flag.String("create-admin", "", "create an admin using a password from stdin (server-local operation)")
	demoMode := flag.String("demo-alarm", "", "server-local manual demo: create, ack, recover, status")
	demoRequest := flag.String("demo-request-id", "", "idempotency key for creating a manual demo")
	demoDevice := flag.String("demo-device", "INV-1001", "existing demo device code")
	demoLevel := flag.String("demo-level", "major", "manual demo level: minor, major, urgent")
	demoID := flag.Int64("demo-alarm-id", 0, "manual demo alarm ID")
	demoActor := flag.String("demo-actor", "", "existing admin/operator username for acknowledgement")
	demoAnalysis := flag.String("demo-analysis", "model", "manual interpretation: fixed or model")
	flag.Parse()
	if *demoMode != "" && (*migrateOnly || *createAdmin != "") {
		log.Fatal("demo_operation_must_not_be_combined_with_other_modes")
	}
	startupBudget, err := migrationStartupBudget(*migrateOnly, *migrationTimeout)
	if err != nil {
		log.Fatal(err)
	}
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
	root, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	startup, cancelStartup := context.WithTimeout(root, startupBudget)
	defer cancelStartup()
	logger.Info("api_startup", zap.String("phase", "schema"))
	pg, err := store.PostgresContext(startup, cfg)
	if err != nil {
		logger.Fatal("postgres", zap.Error(err))
	}
	pg = pg.WithContext(startup)
	if *demoMode != "" {
		if *demoMode == "create" && (!cfg.AgentInterpretEnabled || (*demoAnalysis == "model" && (!cfg.AgentEnabled || cfg.AgentInterpretProtocol != "v2"))) {
			log.Fatal("manual_demo_requires_existing_v2_interpretation_configuration")
		}
		if err := runManualDemo(startup, cfg, pg, *demoMode, *demoRequest, *demoDevice, *demoLevel, *demoID, *demoActor, *demoAnalysis); err != nil {
			log.Fatal(err)
		}
		return
	}
	for _, path := range store.MigrationPaths {
		if err = store.Migrate(pg, path); err != nil {
			logger.Fatal("migration", zap.Error(err))
		}
	}
	if err = store.SeedDemoDevices(pg); err != nil {
		logger.Fatal("seed_devices", zap.Error(err))
	}
	// Refresh the exact fleet identity before adding natural rules. If the
	// manifest or registry is incomplete, keep the API available but do not
	// create an unverified subset of rules. The sample path also fails closed.
	if catalog, loadErr := fleet.Load(cfg.FleetPath); loadErr != nil {
		logger.Warn("fleet_natural_rules_skipped", zap.Error(loadErr))
	} else if report, policyErr := service.EnsureSimulationPolicyDevices(pg, catalog); policyErr != nil {
		logger.Warn("fleet_natural_rules_skipped", zap.Error(policyErr))
	} else if !report.OK {
		logger.Warn("fleet_natural_rules_skipped", zap.Int("expected", report.Expected), zap.Int("verified", report.Verified))
	} else if err = store.Migrate(pg, "deploy/postgres/012_natural_alarm_rules.sql"); err != nil {
		logger.Fatal("natural_alarm_rules_migration", zap.Error(err))
	}
	if *migrateOnly {
		logger.Info("migration_complete")
		return
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
	redis, err := store.RedisContext(startup, cfg)
	if err != nil {
		logger.Fatal("redis", zap.Error(err))
	}
	defer redis.Close()
	td := tdengine.New(cfg)
	if err = td.Init(startup); err != nil {
		logger.Fatal("tdengine", zap.Error(err))
	}
	cancelStartup()
	pg = pg.WithContext(root)
	server := api.New(cfg, pg, redis, td, logger)
	if err = server.Run(root); err != nil {
		logger.Fatal("api", zap.Error(err))
	}
}

// Large-table schema work is an explicit, offline operation. A normal API
// invocation always keeps the S02 thirty-second initialization budget.
func migrationStartupBudget(offline bool, requested time.Duration) (time.Duration, error) {
	if !offline && requested != 30*time.Second {
		return 0, fmt.Errorf("migration-timeout requires migrate-only")
	}
	if requested < 30*time.Second || requested > 15*time.Minute {
		return 0, fmt.Errorf("migration-timeout must be between 30s and 15m")
	}
	return requested, nil
}
