//go:build tr_s04 && windows

package api

import (
	"context"
	"encoding/json"
	"fmt"
	"go.uber.org/zap"
	"os"
	"os/exec"
	"path/filepath"
	"powersystem/backend/internal/model"
	"powersystem/backend/internal/store"
	"powersystem/backend/internal/telemetry"
	"strings"
	"testing"
	"time"
)

func TestTRS04MigrationUpgrade(t *testing.T) {
	f := s04new(t)
	name := fmt.Sprintf("tr_s04_upgrade_%d", time.Now().UnixNano())
	s04must(t, f.db.Exec("CREATE DATABASE "+name).Error)
	old := os.Getenv("TR_S04_OLD_API_BIN")
	if old == "" {
		old = filepath.Join(os.Getenv("TR_S04_EVIDENCE"), "api-baseline.exe")
	}
	migrate := func(bin string) {
		c := exec.Command(bin, "-migrate-only")
		c.Dir = "../.."
		c.Env = append(os.Environ(), "POSTGRES_HOST=127.0.0.1", "POSTGRES_PORT=15442", "POSTGRES_USER=tr_s04", "POSTGRES_DB="+name, "POSTGRES_PASSWORD=fixture-only", "REDIS_PASSWORD=fixture-only", "TDENGINE_ROOT_PASSWORD=fixture-only", "JWT_SECRET="+strings.Repeat("s", 32), "FLEET_CONFIG_PATH=fixture-missing", "AGENT_ENABLED=false", "FEISHU_BUSINESS_ENABLED=false", "SIMULATION_ALARM_MODE=off")
		out, e := c.CombinedOutput()
		if e != nil || !strings.Contains(string(out), "migration_complete") {
			t.Fatalf("actual upgrade %v %s", e, out)
		}
	}
	migrate(old)
	cfg := f.cfg
	cfg.PostgresDSN = strings.Replace(s04DSN, "tr_s04_test", name, 1)
	db, e := store.Postgres(cfg)
	s04must(t, e)
	pool, _ := db.DB()
	defer pool.Close()
	d := model.Device{DeviceCode: "S04-UPGRADE", Name: "legacy fixture", DevType: "inverter", StationCode: "S04"}
	s04must(t, db.Create(&d).Error)
	sample := f.sample(d, "legacy", 1, time.Now().UnixMilli())
	payload, _ := json.Marshal(sample)
	s04must(t, db.Exec(`INSERT INTO telemetry_inbox(run_id,device_id,seq,ts_ms,payload,business_state,processed_at) VALUES(?,?,?,?,?::jsonb,NULL,now())`, sample.RunID, d.ID, sample.Seq, sample.TS, string(payload)).Error)
	s04must(t, db.Exec(`INSERT INTO energy_daily(device_id,day,kwh,segments_count,source_quality) VALUES(?,CURRENT_DATE,123.45,1,'legacy_unknown')`, d.ID).Error)
	for n := 0; n < 2; n++ {
		migrate(os.Getenv("TR_S04_API_BIN"))
	}
	var i model.Inbox
	s04must(t, db.Where("run_id='legacy'").First(&i).Error)
	var kwh float64
	s04must(t, db.Raw("SELECT kwh FROM energy_daily WHERE device_id=?", d.ID).Scan(&kwh).Error)
	if i.BusinessState != nil || i.ProcessedAt == nil || string(s04canonical(i.Payload)) != string(s04canonical(payload)) || kwh != 123.45 {
		t.Fatal("upgrade reclassified/changed legacy", i.BusinessState, kwh)
	}
	s04must(t, f.s.td.Insert(context.Background(), d, []telemetry.Sample{sample}))
	upgraded := New(cfg, db, f.rd, f.s.td, zap.NewNop())
	defer func() {
		if upgraded.realtime.cancel != nil {
			upgraded.realtime.cancel()
			<-upgraded.realtime.done
		}
	}()
	s04must(t, upgraded.restoreLatest(context.Background()))
	var source int64
	s04must(t, db.Raw("SELECT inbox_id FROM telemetry_snapshots WHERE device_id=?", d.ID).Scan(&source).Error)
	if source != i.ID {
		t.Fatal("legacy source not reverified")
	}
	s04must(t, db.Exec("UPDATE alarm_maintenance_state SET enabled=true WHERE id=1").Error)
	if e := upgraded.restoreLatest(context.Background()); e == nil {
		t.Fatal("safe halt rollback failed")
	}
	s04must(t, db.Exec("UPDATE alarm_maintenance_state SET enabled=false WHERE id=1").Error)
	s04must(t, upgraded.restoreLatest(context.Background()))
	t.Log("actual pre-016 application entry -> legacy NULL receipt and 123.45kWh -> current application entry twice; no reclassification/value rewrite, legacy TD reverified, freeze-to-safe-halt and same-schema candidate resume; retained database " + name)
}
func s04canonical(b []byte) []byte {
	var v any
	json.Unmarshal(b, &v)
	out, _ := json.Marshal(v)
	return out
}
