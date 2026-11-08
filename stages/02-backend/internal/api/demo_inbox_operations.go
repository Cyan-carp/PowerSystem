package api

import (
	"math"
	"time"

	"github.com/gin-gonic/gin"
)

// 演示台只读运行快照。复用现成的限时队列探测结果，不在网页刷新时查询大表。
// 过期计数是 API 进程启动后的累计值，不能拿它当业务成功数或历史总数。
func (s *Server) demoConsoleInboxOperations(c *gin.Context) {
	now := time.Now().UTC()
	m := &s.telemetry
	m.mu.Lock()
	queueAt := m.times["inbox_queue"]
	expiryAt := m.times["expiry_run"]
	expiryRows := m.counts["expiry_rows"]
	probeRows := m.counts["expiry_probe_rows"]
	known := m.auxValues["inbox_queue_stats_known"] == 1 && !queueAt.IsZero() && now.Sub(queueAt) <= 3*inboxQueueProbeInterval
	inWindow := m.auxValues["inbox_pending_rows_in_window"]
	outsideWindow := m.auxValues["inbox_pending_rows_outside_window"]
	m.mu.Unlock()
	if probeRows > expiryRows {
		probeRows = expiryRows
	}

	var pendingIn, pendingOutside any
	if known && !math.IsNaN(inWindow) && !math.IsInf(inWindow, 0) && !math.IsNaN(outsideWindow) && !math.IsInf(outsideWindow, 0) {
		pendingIn, pendingOutside = int64(inWindow), int64(outsideWindow)
	} else {
		known = false
	}
	observedAt := now
	if !queueAt.IsZero() {
		observedAt = queueAt.UTC()
	}
	var lastRun any
	if !expiryAt.IsZero() {
		lastRun = expiryAt.UTC().Format(time.RFC3339)
	}
	ok(c, gin.H{
		"observed_at":             observedAt.Format(time.RFC3339),
		"known":                   known,
		"pending_in_window":       pendingIn,
		"pending_outside_window":  pendingOutside,
		"expiry_rows_since_start": expiryRows - probeRows,
		"s02_enabled":             s.expiry.Enabled,
		"s02_last_run_at":         lastRun,
	})
}
