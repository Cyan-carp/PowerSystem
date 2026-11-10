package api

import (
	"bytes"
	"database/sql"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/golang-jwt/jwt/v5"
	"golang.org/x/crypto/bcrypt"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	_ "modernc.org/sqlite"

	"powersystem/backend/internal/config"
	"powersystem/backend/internal/model"
)

func TestAccountSettingsPasswordChangeInvalidatesSessions(t *testing.T) {
	conn, err := sql.Open("sqlite", ":memory:")
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	conn.SetMaxOpenConns(1)
	_, err = conn.Exec(`CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, password_hash TEXT, session_version BIGINT NOT NULL DEFAULT 0, role TEXT, real_name TEXT, created_at DATETIME, updated_at DATETIME)`)
	if err != nil {
		t.Fatal(err)
	}
	db, err := gorm.Open(postgres.New(postgres.Config{Conn: conn}), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		t.Fatal(err)
	}
	for _, user := range []struct {
		id   int64
		name string
		role string
	}{{1, "admin-one", "admin"}, {2, "operator-two", "operator"}} {
		hash, err := bcrypt.GenerateFromPassword([]byte("old-pass-123"), bcrypt.MinCost)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := conn.Exec(`INSERT INTO users (id, username, password_hash, role, real_name) VALUES (?, ?, ?, ?, ?)`, user.id, user.name, string(hash), user.role, user.name); err != nil {
			t.Fatal(err)
		}
	}
	secret := strings.Repeat("s", 32)
	s := &Server{cfg: config.Config{JWTSecret: secret}, db: db}
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.POST("/api/v1/auth/login", s.login)
	a := r.Group("/api/v1", s.authorize)
	a.GET("/auth/me", s.me)
	a.POST("/auth/change-password", s.changePassword)

	call := func(method, path, token string, body any) *httptest.ResponseRecorder {
		t.Helper()
		var raw []byte
		if body != nil {
			raw, _ = json.Marshal(body)
		}
		req := httptest.NewRequest(method, path, bytes.NewReader(raw))
		if token != "" {
			req.Header.Set("Authorization", "Bearer "+token)
		}
		if body != nil {
			req.Header.Set("Content-Type", "application/json")
		}
		w := httptest.NewRecorder()
		r.ServeHTTP(w, req)
		return w
	}
	login := func(name, password string, expected int) string {
		t.Helper()
		w := call(http.MethodPost, "/api/v1/auth/login", "", map[string]string{"username": name, "password": password})
		if w.Code != expected {
			t.Fatalf("login %s: got %d, want %d: %s", name, w.Code, expected, w.Body.String())
		}
		if expected != 200 {
			return ""
		}
		var body struct {
			Data struct {
				Token string `json:"token"`
			} `json:"data"`
		}
		if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil || body.Data.Token == "" {
			t.Fatal("login token missing", err)
		}
		return body.Data.Token
	}
	for _, tc := range []struct{ name, role string }{{"admin-one", "admin"}, {"operator-two", "operator"}} {
		t.Run(tc.role, func(t *testing.T) {
			first := login(tc.name, "old-pass-123", 200)
			second := login(tc.name, "old-pass-123", 200)
			legacy, err := jwt.NewWithClaims(jwt.SigningMethodHS256, jwt.MapClaims{"sub": map[string]string{"admin-one": "1", "operator-two": "2"}[tc.name], "exp": time.Now().Add(time.Hour).Unix()}).SignedString([]byte(secret))
			if err != nil {
				t.Fatal(err)
			}
			for _, token := range []string{first, legacy} {
				w := call(http.MethodGet, "/api/v1/auth/me", token, nil)
				if w.Code != 200 || !strings.Contains(w.Body.String(), tc.name) || strings.Contains(w.Body.String(), "password_hash") || strings.Contains(w.Body.String(), "session_version") {
					t.Fatalf("me response invalid: %d %s", w.Code, w.Body.String())
				}
			}
			userID := map[string]int64{"admin-one": 1, "operator-two": 2}[tc.name]
			if id, err := s.ticketUser(map[string]string{"admin-one": "1:0", "operator-two": "2:0"}[tc.name]); err != nil || id != userID {
				t.Fatal("current session ticket rejected", err)
			}
			change := func(current, next string) *httptest.ResponseRecorder {
				return call(http.MethodPost, "/api/v1/auth/change-password?user_id=999", first, map[string]string{"current_password": current, "new_password": next})
			}
			for _, bad := range []struct {
				current, next string
				status        int
			}{{"wrong-pass", "new-pass-456", 400}, {"old-pass-123", "short", 400}, {"old-pass-123", "old-pass-123", 400}, {"old-pass-123", strings.Repeat("x", 73), 400}} {
				if w := change(bad.current, bad.next); w.Code != bad.status {
					t.Fatalf("bad change: %d %s", w.Code, w.Body.String())
				}
			}
			if w := call(http.MethodGet, "/api/v1/auth/me", second, nil); w.Code != 200 {
				t.Fatal("failed change invalidated session")
			}
			if tc.role == "operator" {
				if _, err := conn.Exec(`CREATE TRIGGER fail_password_change BEFORE UPDATE ON users WHEN NEW.id = 2 BEGIN SELECT RAISE(ABORT, 'fixture write failure'); END`); err != nil {
					t.Fatal(err)
				}
				if w := change("old-pass-123", "new-pass-456"); w.Code != 500 {
					t.Fatalf("failed write returned %d", w.Code)
				}
				if _, err := conn.Exec(`DROP TRIGGER fail_password_change`); err != nil {
					t.Fatal(err)
				}
				if w := call(http.MethodGet, "/api/v1/auth/me", second, nil); w.Code != 200 {
					t.Fatal("failed write invalidated session")
				}
			}
			if w := change("old-pass-123", "new-pass-456"); w.Code != 200 {
				t.Fatalf("change failed: %d %s", w.Code, w.Body.String())
			}
			if _, err := s.ticketUser(map[string]string{"admin-one": "1:0", "operator-two": "2:0"}[tc.name]); err == nil {
				t.Fatal("old session ticket accepted")
			}
			if id, err := s.ticketUser(map[string]string{"admin-one": "1:1", "operator-two": "2:1"}[tc.name]); err != nil || id != userID {
				t.Fatal("new session ticket rejected", err)
			}
			if tc.role == "admin" {
				var untouched model.User
				if err := db.First(&untouched, 2).Error; err != nil || untouched.SessionVersion != 0 {
					t.Fatalf("other account changed: %d %v", untouched.SessionVersion, err)
				}
			}
			for _, token := range []string{first, second, legacy} {
				if w := call(http.MethodGet, "/api/v1/auth/me", token, nil); w.Code != 401 {
					t.Fatalf("old token accepted: %d", w.Code)
				}
			}
			login(tc.name, "old-pass-123", 401)
			fresh := login(tc.name, "new-pass-456", 200)
			if w := call(http.MethodGet, "/api/v1/auth/me", fresh, nil); w.Code != 200 {
				t.Fatalf("new token rejected: %d", w.Code)
			}
		})
	}
	var other model.User
	if err := db.First(&other, 2).Error; err != nil || other.SessionVersion != 1 {
		t.Fatalf("unexpected operator version: %d %v", other.SessionVersion, err)
	}
}
