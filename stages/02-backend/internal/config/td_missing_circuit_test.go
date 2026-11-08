package config

import (
	"strings"
	"testing"
)

func tdMissingRequiredSecrets(t *testing.T) {
	t.Helper()
	t.Setenv("POSTGRES_PASSWORD", "fixture-only")
	t.Setenv("REDIS_PASSWORD", "fixture-only")
	t.Setenv("TDENGINE_ROOT_PASSWORD", "fixture-only")
	t.Setenv("JWT_SECRET", strings.Repeat("x", 32))
	t.Setenv("SIMULATION_ALARM_MODE", "off")
}

// IR-S03 与 S02 一样默认关闭：未显式设置时不能改变既有 TD 查询路径。
func TestTDMissingCircuitDefaultsOff(t *testing.T) {
	tdMissingRequiredSecrets(t)
	cfg, err := Load("api")
	if err != nil {
		t.Fatal(err)
	}
	if cfg.TDMissingCircuitEnabled {
		t.Fatal("TD_MISSING_CIRCUIT_ENABLED must default to false")
	}
	t.Setenv("TD_MISSING_CIRCUIT_ENABLED", "true")
	cfg, err = Load("api")
	if err != nil {
		t.Fatal(err)
	}
	if !cfg.TDMissingCircuitEnabled {
		t.Fatal("TD_MISSING_CIRCUIT_ENABLED=true was ignored")
	}
}
