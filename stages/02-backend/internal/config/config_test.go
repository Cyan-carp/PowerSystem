package config

import (
	"strings"
	"testing"
)

func setRequiredSecrets(t *testing.T) {
	t.Helper()
	t.Setenv("POSTGRES_PASSWORD", "fixture-only")
	t.Setenv("REDIS_PASSWORD", "fixture-only")
	t.Setenv("TDENGINE_ROOT_PASSWORD", "fixture-only")
	t.Setenv("JWT_SECRET", strings.Repeat("x", 32))
	t.Setenv("SIMULATION_ALARM_MODE", "off")
}

// 灰度开关默认关闭是 S02 的施工边界：未显式启用时不得启动过期写路径。
func TestInboxExpiryDefaultsOff(t *testing.T) {
	setRequiredSecrets(t)
	cfg, err := Load("api")
	if err != nil {
		t.Fatal(err)
	}
	if cfg.InboxExpiryEnabled {
		t.Fatal("INBOX_EXPIRY_ENABLED must default to false")
	}
	if cfg.InboxExpiryIntervalSeconds != 30 || cfg.InboxExpiryBatch != 2000 || cfg.InboxExpiryBudgetSeconds != 5 {
		t.Fatalf("frozen defaults drifted: %+v", cfg)
	}
}

func TestInboxExpiryRejectsUnsafeBounds(t *testing.T) {
	setRequiredSecrets(t)
	for _, tc := range []struct {
		name, key, value string
	}{
		{"interval too small", "INBOX_EXPIRY_INTERVAL_SECONDS", "4"},
		{"batch zero", "INBOX_EXPIRY_BATCH", "0"},
		{"budget too large", "INBOX_EXPIRY_BUDGET_SECONDS", "31"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv(tc.key, tc.value)
			if _, err := Load("api"); err == nil {
				t.Fatalf("%s=%s should be rejected", tc.key, tc.value)
			}
		})
	}
}
