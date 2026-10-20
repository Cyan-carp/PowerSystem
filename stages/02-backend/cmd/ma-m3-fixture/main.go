// Explicit isolated MA-M3 authentication/platform fixture; never use as a runtime entry.
package main

import (
	"fmt"
	"golang.org/x/crypto/bcrypt"
	"os"
	"path/filepath"
	"powersystem/backend/internal/config"
	"powersystem/backend/internal/store"
	"strings"
)

func main() {
	cfg, err := config.Load("api")
	if err != nil || os.Getenv("POSTGRES_DB") != "ma_m3_business_test" || os.Getenv("POSTGRES_HOST") != "127.0.0.1" {
		panic("dedicated_m3_fixture_required")
	}
	db, err := store.Postgres(cfg)
	if err != nil {
		panic("fixture_database_unavailable")
	}
	sqlDB, _ := db.DB()
	defer sqlDB.Close()
	for _, name := range []string{"001_init.sql", "002_predictions.sql", "003_energy.sql", "004_business_notifications.sql", "005_agent_interpretations.sql", "006_alarm_statistics.sql", "007_alarm_closure.sql", "008_alarm_archive.sql"} {
		if store.Migrate(db, filepath.Join("deploy/postgres", name)) != nil {
			panic("fixture_migration_failed")
		}
	}
	for _, entry := range []struct{ name, role, key string }{{"ma_m3_admin", "admin", "MA_M3_ADMIN_PASSWORD_FILE"}, {"ma_m3_operator", "operator", "MA_M3_OPERATOR_PASSWORD_FILE"}} {
		raw, e := os.ReadFile(os.Getenv(entry.key))
		if e != nil {
			panic("private_fixture_password_required")
		}
		password := strings.TrimSpace(string(raw))
		if len(password) < 24 {
			panic("fixture_password_too_short")
		}
		hash, e := bcrypt.GenerateFromPassword([]byte(password), bcrypt.DefaultCost)
		if e != nil {
			panic("fixture_hash_failed")
		}
		if db.Exec("INSERT INTO users(username,password_hash,role,real_name) VALUES(?,?,?,?) ON CONFLICT(username) DO NOTHING", entry.name, string(hash), entry.role, "MA-M3 isolated fixture").Error != nil {
			panic("fixture_user_failed")
		}
	}
	if store.SeedDemoDevices(db) != nil {
		panic("fixture_devices_failed")
	}
	fmt.Println("Dedicated MA-M3 fixture initialized: two test roles and synthetic devices; no ordinary business database touched.")
}
