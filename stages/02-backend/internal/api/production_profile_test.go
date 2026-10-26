package api

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"powersystem/backend/internal/config"
)

const (
	v4TestMachineID = "synthetic-machine-id-raw-bytes\n"
	v4TestProduct   = "4c4c4544-0037-3010-8058-cac04f4d4d32\n"
)

func v4ProductionIdentityFixture(t *testing.T) (string, map[string]any) {
	t.Helper()
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "machine-id"), []byte(v4TestMachineID), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "product-uuid"), []byte(v4TestProduct), 0o600); err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256([]byte(v4TestMachineID))
	return root, map[string]any{
		"profile": v4ProductionProfile,
		"host": map[string]any{
			"hostname":          "powersystem-prod",
			"vm_id":             117,
			"machine_id_sha256": hex.EncodeToString(sum[:]),
			"dmi_uuid":          strings.TrimSpace(v4TestProduct),
		},
		"databases":    map[string]any{"business": "powersystem", "rag": "powersystem_agent_rag", "ops": "ma_ops"},
		"origins":      map[string]any{"api": "http://api:8080", "rag": "http://agent-rag:8093", "ops": "http://ops-agent:8094", "security": "http://security-agent:8100"},
		"capabilities": map[string]any{"actions": true, "schedules": true, "ops_write": false},
		"policies":     map[string]any{"business": v4BusinessProductionPolicy, "schedule": v4ScheduleProductionPolicy},
	}
}

func useV4ProductionFixture(t *testing.T) string {
	t.Helper()
	root, doc := v4ProductionIdentityFixture(t)
	writeV4ProductionIdentity(t, root, doc)
	previous := v4ProductionIdentityRoot
	v4ProductionIdentityRoot = root
	t.Cleanup(func() { v4ProductionIdentityRoot = previous })
	t.Setenv("V4_PRODUCTION_PROFILE", v4ProductionProfile)
	return root
}

func writeV4ProductionIdentity(t *testing.T, root string, doc map[string]any) {
	t.Helper()
	raw, err := json.Marshal(doc)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "identity.json"), raw, 0o600); err != nil {
		t.Fatal(err)
	}
}

func v4ProductionTestConfig(t *testing.T) config.Config {
	t.Helper()
	t.Setenv("V4_ACTIONS_ENABLED", "true")
	t.Setenv("V4_SCHEDULES_ENABLED", "true")
	t.Setenv("V4_OPS_WRITE_ENABLED", "false")
	bridge := filepath.Join(t.TempDir(), "schedule-bridge-token")
	if err := os.WriteFile(bridge, []byte(strings.Repeat("b", 40)), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("SCHEDULE_BRIDGE_TOKEN_FILE", bridge)
	t.Setenv("V4_JOINT_PROFILE", "")
	return config.Config{
		PostgresDSN:  "postgres://powersystem:synthetic@postgres:5432/powersystem?sslmode=disable&timezone=UTC",
		AgentChatURL: "http://agent-rag:8093",
		OpsURL:       "http://ops-agent:8094",
		AgentToken:   strings.Repeat("a", 40),
		OpsToken:     strings.Repeat("o", 40),
		MAActorKey:   strings.Repeat("m", 40),
	}
}

func TestV4ProductionIdentityExact(t *testing.T) {
	root, base := v4ProductionIdentityFixture(t)
	previous := v4ProductionIdentityRoot
	v4ProductionIdentityRoot = root
	t.Cleanup(func() { v4ProductionIdentityRoot = previous })
	t.Setenv("V4_PRODUCTION_PROFILE", v4ProductionProfile)
	writeV4ProductionIdentity(t, root, base)

	if _, err := loadV4ProductionIdentity(); err != nil {
		t.Fatalf("valid identity rejected: %v", err)
	}
	for _, tc := range []struct {
		name   string
		mutate func(map[string]any)
	}{
		{"profile", func(d map[string]any) { d["profile"] = "v4-s09-local" }},
		{"unknown_top", func(d map[string]any) { d["extra"] = true }},
		{"missing_policies", func(d map[string]any) { delete(d, "policies") }},
		{"hostname", func(d map[string]any) { d["host"].(map[string]any)["hostname"] = "other-host" }},
		{"vm", func(d map[string]any) { d["host"].(map[string]any)["vm_id"] = 118 }},
		{"unknown_host", func(d map[string]any) { d["host"].(map[string]any)["extra"] = "x" }},
		{"digest", func(d map[string]any) { d["host"].(map[string]any)["machine_id_sha256"] = strings.Repeat("0", 64) }},
		{"dmi", func(d map[string]any) { d["host"].(map[string]any)["dmi_uuid"] = "other-uuid" }},
		{"database", func(d map[string]any) { d["databases"].(map[string]any)["ops"] = "powersystem" }},
		{"origin", func(d map[string]any) { d["origins"].(map[string]any)["rag"] = "http://ops-agent:8094" }},
		{"capability", func(d map[string]any) { d["capabilities"].(map[string]any)["ops_write"] = true }},
		{"policy", func(d map[string]any) { d["policies"].(map[string]any)["business"] = businessPolicy }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			doc, err := json.Marshal(base)
			if err != nil {
				t.Fatal(err)
			}
			var clone map[string]any
			if err := json.Unmarshal(doc, &clone); err != nil {
				t.Fatal(err)
			}
			tc.mutate(clone)
			writeV4ProductionIdentity(t, root, clone)
			if _, err := loadV4ProductionIdentity(); err == nil {
				t.Fatal("invalid identity accepted")
			}
		})
	}
	writeV4ProductionIdentity(t, root, base)
	if err := os.WriteFile(filepath.Join(root, "machine-id"), []byte("changed-raw-bytes\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := loadV4ProductionIdentity(); err == nil {
		t.Fatal("changed raw machine-id accepted")
	}
}

func TestV4ProductionConfigExact(t *testing.T) {
	useV4ProductionFixture(t)
	if err := (&Server{cfg: v4ProductionTestConfig(t)}).v4ProductionConfig(); err != nil {
		t.Fatalf("valid production configuration rejected: %v", err)
	}
	for _, tc := range []struct {
		name   string
		mutate func(*config.Config)
		env    func(*testing.T)
	}{
		{"default_port", func(c *config.Config) {
			c.PostgresDSN = "postgres://powersystem:synthetic@postgres/powersystem?sslmode=disable&timezone=UTC"
		}, nil},
		{"wrong_port", func(c *config.Config) {
			c.PostgresDSN = "postgres://powersystem:synthetic@postgres:5433/powersystem?sslmode=disable&timezone=UTC"
		}, nil},
		{"wrong_host", func(c *config.Config) {
			c.PostgresDSN = "postgres://powersystem:synthetic@127.0.0.1:5432/powersystem?sslmode=disable&timezone=UTC"
		}, nil},
		{"wrong_database", func(c *config.Config) {
			c.PostgresDSN = "postgres://powersystem:synthetic@postgres:5432/ma_ops?sslmode=disable&timezone=UTC"
		}, nil},
		{"unknown_query", func(c *config.Config) {
			c.PostgresDSN = "postgres://powersystem:synthetic@postgres:5432/powersystem?sslmode=disable&timezone=UTC&options=-csearch_path%3Dx"
		}, nil},
		{"fragment", func(c *config.Config) {
			c.PostgresDSN = "postgres://powersystem:synthetic@postgres:5432/powersystem?sslmode=disable&timezone=UTC#fragment"
		}, nil},
		{"rag_origin", func(c *config.Config) { c.AgentChatURL = "http://127.0.0.1:8093" }, nil},
		{"ops_origin", func(c *config.Config) { c.OpsURL = "http://ops-agent:8095" }, nil},
		{"short_agent_token", func(c *config.Config) { c.AgentToken = "short" }, nil},
		{"joint_profile", func(c *config.Config) {}, func(t *testing.T) { t.Setenv("V4_JOINT_PROFILE", "v4-s09-local") }},
		{"short_bridge", func(c *config.Config) {}, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "short")
			if err := os.WriteFile(path, []byte("short"), 0o600); err != nil {
				t.Fatal(err)
			}
			t.Setenv("SCHEDULE_BRIDGE_TOKEN_FILE", path)
		}},
		{"shared_bridge", func(c *config.Config) {}, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "shared")
			if err := os.WriteFile(path, []byte(strings.Repeat("a", 40)), 0o600); err != nil {
				t.Fatal(err)
			}
			t.Setenv("SCHEDULE_BRIDGE_TOKEN_FILE", path)
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			cfg := v4ProductionTestConfig(t)
			tc.mutate(&cfg)
			if tc.env != nil {
				tc.env(t)
			}
			if err := (&Server{cfg: cfg}).v4ProductionConfig(); err == nil {
				t.Fatal("invalid production configuration accepted")
			}
		})
	}
}

func businessTestToken(t *testing.T, key, policy string) string {
	t.Helper()
	raw := map[string]any{
		"aud": "ma-business-execute/v1", "iat": time.Now().Unix(), "exp": time.Now().Unix() + 60,
		"owner_id": int64(1), "run_id": "58f2c23b-2700-481c-90a2-46042fd1a92b",
		"action_id": "941d4a44-6820-4e37-9cf2-700745e1c4be", "execution_ref": "481897bb-77b9-4dc6-9e5d-b4dcfc343b50",
		"capability": "device.remark.read", "typed_args": map[string]any{"device_id": 1},
		"target_version": "0", "plan_hash": strings.Repeat("a", 64), "policy_version": policy,
	}
	encoded := base64.RawURLEncoding.EncodeToString(businessJSON(raw))
	mac := hmac.New(sha256.New, []byte(key))
	_, _ = mac.Write([]byte(encoded))
	return encoded + "." + base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
}

func TestV4BusinessPolicyIsolation(t *testing.T) {
	key := strings.Repeat("k", 40)
	local := businessTestToken(t, key, businessPolicy)
	production := businessTestToken(t, key, v4BusinessProductionPolicy)
	if _, err := parseBusinessGrantPolicy(local, key, businessPolicy); err != nil {
		t.Fatal("local grant rejected in local domain")
	}
	if _, err := parseBusinessGrantPolicy(production, key, v4BusinessProductionPolicy); err != nil {
		t.Fatal("production grant rejected in production domain")
	}
	if _, err := parseBusinessGrantPolicy(local, key, v4BusinessProductionPolicy); err == nil {
		t.Fatal("local grant accepted in production domain")
	}
	if _, err := parseBusinessGrantPolicy(production, key, businessPolicy); err == nil {
		t.Fatal("production grant accepted in local domain")
	}
	if _, err := parseBusinessGrant(local, key); err != nil {
		t.Fatal("local default signature changed")
	}
	if _, err := parseBusinessGrant(production, key); err == nil {
		t.Fatal("local default accepted a production policy")
	}
}

func TestV4ProductionNoLocalFallback(t *testing.T) {
	root, _ := v4ProductionIdentityFixture(t)
	previous := v4ProductionIdentityRoot
	v4ProductionIdentityRoot = root
	t.Cleanup(func() { v4ProductionIdentityRoot = previous })

	local := config.Config{PostgresDSN: "postgresql://s08_test@127.0.0.1:25438/v4_s08_20261024_r1_test", AgentChatURL: "http://127.0.0.1:28981", OpsURL: "http://127.0.0.1:28983"}
	t.Setenv("V4_S08_LOCAL_ENABLED", "true")
	t.Setenv("V4_PRODUCTION_PROFILE", "v4-s06-local/v1")
	if domain := (&Server{cfg: local}).scheduleReadDomain(); domain != "" {
		t.Fatalf("wrong production profile fell back to %q", domain)
	}

	t.Setenv("V4_PRODUCTION_PROFILE", "")
	if domain := (&Server{cfg: local}).scheduleReadDomain(); domain != "local" {
		t.Fatalf("S08 local guard changed: %q", domain)
	}

	useV4ProductionFixture(t)
	cfg := v4ProductionTestConfig(t)
	if (&Server{cfg: cfg}).v4Production() {
		t.Fatal("production without a live database accepted")
	}
	t.Setenv("V4_JOINT_PROFILE", "v4-s09-local")
	if domain := (&Server{cfg: cfg}).scheduleReadDomain(); domain != "" {
		t.Fatalf("production profile fell back to joint %q", domain)
	}
}
