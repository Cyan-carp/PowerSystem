package api

import (
	"powersystem/backend/internal/config"
	"testing"
)

func TestS08LocalDSNGuard(t *testing.T) {
	t.Setenv("V4_S08_LOCAL_ENABLED", "true")
	base := config.Config{AgentChatURL: "http://127.0.0.1:28981", OpsURL: "http://127.0.0.1:28983"}
	for _, tc := range []struct {
		dsn  string
		want bool
	}{
		{"postgresql://s08_test@127.0.0.1:25438/v4_s08_20261024_r1_test", true},
		{"postgresql://s08_test@production:5432/v4_s08_20261024_r1_test", false},
		{"postgresql://s08_test@127.0.0.1:25438/production", false},
		{"postgresql://s08_test@production:5432/spoof127.0.0.1:25438/v4_s08_test", false},
	} {
		s := Server{cfg: base}
		s.cfg.PostgresDSN = tc.dsn
		if s.s08Local() != tc.want {
			t.Errorf("guard mismatch for %s", tc.dsn)
		}
	}
}
