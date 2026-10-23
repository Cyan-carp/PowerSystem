package api

import (
	"context"
	"net/http"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
)

// A database error is fail-closed. MQTT receipts and the gateway TDengine writer
// remain active; only processing that can mutate alarms or deliver cards pauses.
func (s *Server) alarmMaintenance(ctx context.Context) bool {
	if s.db == nil {
		return false
	}
	var state struct{ Enabled bool }
	check, cancel := context.WithTimeout(ctx, time.Second)
	defer cancel()
	err := s.db.WithContext(check).Raw("SELECT enabled FROM alarm_maintenance_state WHERE id=1").Scan(&state).Error
	return err != nil || state.Enabled
}

func (s *Server) alarmMaintenanceGuard(c *gin.Context) {
	if c.Request.Method == http.MethodGet {
		return
	}
	path := c.Request.URL.Path
	affected := strings.HasPrefix(path, "/api/v1/alarms") || strings.HasPrefix(path, "/api/v1/alarm-") ||
		strings.HasPrefix(path, "/api/v1/agent/model-probe") ||
		strings.HasPrefix(path, "/api/v1/devices")
	if affected && s.alarmMaintenance(c.Request.Context()) {
		fail(c, 503, 50301, "alarm maintenance in progress")
	}
}
