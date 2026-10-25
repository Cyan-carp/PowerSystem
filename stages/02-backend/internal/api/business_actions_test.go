package api

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"powersystem/backend/internal/config"
	"strings"
	"testing"
	"time"
)

func TestBusinessGrantRequiresTypedBinding(t *testing.T) {
	key := "synthetic-unit-actor-key-32-chars-0000"
	raw := map[string]any{"aud": "ma-business-execute/v1", "iat": time.Now().Unix(), "exp": time.Now().Unix() + 60, "owner_id": int64(1), "run_id": "58f2c23b-2700-481c-90a2-46042fd1a92b", "action_id": "941d4a44-6820-4e37-9cf2-700745e1c4be", "execution_ref": "481897bb-77b9-4dc6-9e5d-b4dcfc343b50", "capability": "device.remark", "typed_args": map[string]any{"device_id": 1, "remark": "<plain>\u2028😀", "expected_version": 0}, "target_version": "0", "plan_hash": strings.Repeat("a", 64), "policy_version": businessPolicy}
	encoded := base64.RawURLEncoding.EncodeToString(businessJSON(raw))
	mac := hmac.New(sha256.New, []byte(key))
	_, _ = mac.Write([]byte(encoded))
	token := encoded + "." + base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
	grant, err := parseBusinessGrant(token, key)
	if err != nil {
		t.Fatal(err)
	}
	id, text, version, args, err := businessArgs(grant)
	if err != nil || id != 1 || text != "<plain>\u2028😀" || version != 0 {
		t.Fatal("typed values changed", err)
	}
	if string(businessJSON(args)) != "{\"device_id\":1,\"expected_version\":0,\"remark\":\"<plain>\u2028😀\"}" {
		t.Fatal("canonical string changed")
	}
	if _, err := parseBusinessGrant(token+"x", key); err == nil {
		t.Fatal("tampered token accepted")
	}
	grant.Args = json.RawMessage(`{"device_id":1,"expected_version":0,"new_remark":"alias"}`)
	if _, _, _, _, err := businessArgs(grant); err == nil {
		t.Fatal("unknown alias accepted")
	}
}

func TestBusinessPolicyCannotEnableProduction(t *testing.T) {
	t.Setenv("V4_S06_LOCAL_ENABLED", "true")
	for _, dsn := range []string{"postgresql://postgres:5432/ma_ops", "postgresql://127.0.0.1:5432/v4_s06_remote_test"} {
		s := &Server{cfg: config.Config{PostgresDSN: dsn}}
		if s.businessLocal() {
			t.Fatal("production or non-S06 port accepted")
		}
	}
	t.Setenv("V4_S06_LOCAL_ENABLED", "false")
	s := &Server{cfg: config.Config{PostgresDSN: "postgresql://127.0.0.1:25436/v4_s06_disabled_test"}}
	if s.businessLocal() {
		t.Fatal("disabled switch accepted")
	}
}
