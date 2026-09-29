package api

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/gin-gonic/gin"
)

func TestRequireAdmin(t *testing.T) {
	gin.SetMode(gin.TestMode)
	for _, tc := range []struct {
		role string
		want int
	}{{"admin", 200}, {"operator", 403}, {"", 403}} {
		r := gin.New()
		r.GET("/write", func(c *gin.Context) {
			if tc.role != "" {
				c.Set("user_role", tc.role)
			}
			c.Next()
		}, requireAdmin, func(c *gin.Context) { c.Status(http.StatusOK) })
		w := httptest.NewRecorder()
		r.ServeHTTP(w, httptest.NewRequest("GET", "/write", nil))
		if w.Code != tc.want {
			t.Fatalf("role=%q got %d want %d", tc.role, w.Code, tc.want)
		}
	}
}

func TestAuthorizeMissingToken(t *testing.T) {
	gin.SetMode(gin.TestMode)
	s := &Server{}
	r := gin.New()
	r.GET("/private", s.authorize, func(c *gin.Context) { c.Status(http.StatusOK) })
	w := httptest.NewRecorder()
	r.ServeHTTP(w, httptest.NewRequest("GET", "/private", nil))
	if w.Code != 401 {
		t.Fatalf("got %d want 401", w.Code)
	}
}
