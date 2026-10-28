package api

import (
	"bytes"
	"context"
	"net/http"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm"
	"powersystem/backend/internal/service"
)

// Unknown configuration is fail-closed. MQTT receipts and the gateway TDengine
// writer remain active; business consumption and derived cache writes pause.
func (s *Server) maintenanceState(ctx context.Context) string {
	if s.db == nil {
		return "sql_error"
	}
	var state struct{ Enabled bool }
	check, cancel := context.WithTimeout(ctx, time.Second)
	defer cancel()
	r := s.db.WithContext(check).Raw("SELECT enabled FROM alarm_maintenance_state WHERE id=1").Scan(&state)
	if r.Error != nil {
		return "sql_error"
	}
	if r.RowsAffected != 1 {
		return "missing"
	}
	if state.Enabled {
		return "enabled"
	}
	return "disabled"
}
func (s *Server) alarmMaintenance(ctx context.Context) bool {
	state := s.maintenanceState(ctx)
	s.telemetry.touch("gate_"+state, time.Now())
	if state != "disabled" {
		s.telemetryLog("maintenance_"+state, time.Now())
	}
	return state != "disabled"
}

func (s *Server) alarmMaintenanceGuard(c *gin.Context) {
	if c.Request.Method == http.MethodGet {
		return
	}
	path := c.Request.URL.Path
	affected := strings.HasPrefix(path, "/api/v1/alarms") || strings.HasPrefix(path, "/api/v1/alarm-") ||
		strings.HasPrefix(path, "/api/v1/agent/model-probe") ||
		strings.HasPrefix(path, "/api/v1/devices")
	if !affected {
		return
	}
	if s.alarmMaintenance(c.Request.Context()) {
		fail(c, 503, 50301, "alarm maintenance in progress")
		return
	}
	// Approved actions own their receipt transaction and the same write gate.
	// Do not put an unrelated outer commit around that durable receipt.
	if c.Request.Method == http.MethodPatch && strings.HasSuffix(path, "/remark") {
		return
	}
	// Buffer the response until the request-local transaction commits. Handlers
	// use requestDB, so no shared Server database/mutex fields are copied.
	ctx, cancel := context.WithTimeout(c.Request.Context(), inboxTransactionBudget)
	defer cancel()
	c.Request = c.Request.WithContext(ctx)
	tx := s.db.WithContext(ctx).Begin()
	if tx.Error != nil {
		fail(c, 503, 50301, "maintenance admission unavailable")
		return
	}
	defer tx.Rollback()
	if err := service.AlarmWriteGate(tx); err != nil {
		fail(c, 503, 50301, "alarm maintenance in progress")
		return
	}
	original := c.Writer
	defer func() { c.Writer = original }()
	buffered := &maintenanceResponse{ResponseWriter: original, status: 200}
	c.Writer = buffered
	c.Set("maintenance_db", tx)
	c.Next()
	c.Writer = original
	if buffered.status >= 400 {
		tx.Rollback()
	} else if ctx.Err() != nil {
		fail(c, 503, 50301, "write cancelled")
		return
	} else if err := tx.Commit().Error; err != nil {
		s.requestRealtimeRefresh()
		fail(c, 503, 50301, "write outcome requires read-only verification")
		return
	}
	if buffered.status < 400 {
		if value, ok := c.Get("maintenance_events"); ok {
			for _, event := range value.([]any) {
				s.broadcast(event)
			}
		}
	}
	original.WriteHeader(buffered.status)
	original.Write(buffered.body.Bytes())
}
func (s *Server) requestDB(c *gin.Context) *gorm.DB {
	if db, ok := c.Get("maintenance_db"); ok {
		return db.(*gorm.DB)
	}
	return s.db.WithContext(c.Request.Context())
}

type maintenanceResponse struct {
	gin.ResponseWriter
	body    bytes.Buffer
	status  int
	written bool
}

func (w *maintenanceResponse) WriteHeader(code int)        { w.status = code }
func (w *maintenanceResponse) WriteHeaderNow()             { w.written = true }
func (w *maintenanceResponse) Write(p []byte) (int, error) { w.written = true; return w.body.Write(p) }
func (w *maintenanceResponse) WriteString(p string) (int, error) {
	w.written = true
	return w.body.WriteString(p)
}
func (w *maintenanceResponse) Status() int   { return w.status }
func (w *maintenanceResponse) Size() int     { return w.body.Len() }
func (w *maintenanceResponse) Written() bool { return w.written }
func (w *maintenanceResponse) Flush()        {}

func (s *Server) broadcastAfterWrite(c *gin.Context, event any) {
	if _, ok := c.Get("maintenance_db"); !ok {
		s.broadcast(event)
		return
	}
	var events []any
	if v, ok := c.Get("maintenance_events"); ok {
		events = v.([]any)
	}
	c.Set("maintenance_events", append(events, event))
}
