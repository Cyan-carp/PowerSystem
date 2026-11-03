package api

import (
	"strings"
	"testing"
)

// 取任务与事务内复核必须同向。2026-11-03 把 inboxHeads 改成「每设备最新优先」
// 后忘了改 server.go 的队头复核，复核仍按最旧取，于是每条样本都被判成「不是
// 队头」，走 errInboxDeferred 静默返回：调度器照常选行、游标照常推进、日志没有
// 任何错误，但成功数长期为 0，入库停摆 40 分钟。
func TestInboxHeadProbeMatchesSelectionDirection(t *testing.T) {
	if !strings.Contains(inboxHeadProbeSQL, "ORDER BY id DESC") {
		t.Fatalf("队头复核必须与最新优先同向，当前为: %s", inboxHeadProbeSQL)
	}
	if strings.Contains(inboxHeadProbeSQL, "ORDER BY id LIMIT") {
		t.Fatalf("队头复核退回了最旧优先: %s", inboxHeadProbeSQL)
	}
}
