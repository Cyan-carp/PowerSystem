package api

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"github.com/gin-gonic/gin"
	"net/http/httptest"
	"powersystem/backend/internal/config"
	"strings"
	"testing"
)

func TestMAContextBindsTrustedActorAndUnicodeRequest(t *testing.T) {
	gin.SetMode(gin.TestMode)
	c, _ := gin.CreateTestContext(httptest.NewRecorder())
	c.Set("user_id", int64(123))
	c.Set("user_role", "operator")
	key := strings.Repeat("k", 32)
	s := Server{cfg: config.Config{MAActorKey: key}}
	sid := newMAUUID()
	token := s.maContext(c, sid, "第二台设备 <current>")
	parts := strings.Split(token, ".")
	mac := hmac.New(sha256.New, []byte(key))
	mac.Write([]byte(parts[0]))
	if hex.EncodeToString(mac.Sum(nil)) != parts[1] {
		t.Fatal("invalid signature")
	}
	raw, _ := base64.RawURLEncoding.DecodeString(parts[0])
	var v map[string]any
	json.Unmarshal(raw, &v)
	h := sha256.Sum256([]byte("第二台设备 <current>"))
	if v["session_id"] != sid || v["role"] != "operator" || v["request_hash"] != hex.EncodeToString(h[:]) || v["user_id"] != float64(123) {
		t.Fatal("context mismatch")
	}
	if !validSessionID(v["trace_id"].(string)) {
		t.Fatal("invalid trace UUID")
	}
}

func TestMAIdentityRequiresDedicatedServiceCredential(t *testing.T) {
	gin.SetMode(gin.TestMode)
	s := Server{cfg: config.Config{OpsToken: strings.Repeat("s", 32), MAActorKey: strings.Repeat("k", 32)}}
	r := gin.New()
	r.GET("/internal/ma/v1/actor", s.maActor)
	w := httptest.NewRecorder()
	r.ServeHTTP(w, httptest.NewRequest("GET", "/internal/ma/v1/actor", nil))
	if w.Code != 403 {
		t.Fatalf("got %d", w.Code)
	}
}
