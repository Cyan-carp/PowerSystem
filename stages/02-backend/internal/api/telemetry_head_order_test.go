package api

import (
	"strings"
	"testing"
)

// 取任务与事务内复核必须同向、同谓词。2026-11-03 把 inboxHeads 改成「每设备最新
// 优先」后忘了改 server.go 的队头复核，复核仍按最旧取，于是每条样本都被判成「不是
// 队头」，走 errInboxDeferred 静默返回：调度器照常选行、游标照常推进、日志没有
// 任何错误，但成功数长期为 0，入库停摆 40 分钟。
//
// 2026-11-07 起方向改回先进先出（id ASC），由 7 天时间窗兜底——窗口保证队头不会被
// 永远好不了的旧消息占死，所以不会重演那次死锁。方向与窗口是一对：去掉窗口就不能
// 改回 ASC（会立刻堵回 11-03 的状态），改回 DESC 则会继续漏样本。
func TestInboxHeadProbeMatchesSelectionDirection(t *testing.T) {
	if !strings.Contains(inboxHeadProbeSQL, "ORDER BY id ASC") {
		t.Fatalf("队头复核必须与先进先出同向，当前为: %s", inboxHeadProbeSQL)
	}
	if strings.Contains(inboxHeadProbeSQL, "ORDER BY id DESC") {
		t.Fatalf("队头复核退回最新优先，会与取任务漂移: %s", inboxHeadProbeSQL)
	}
	if !strings.Contains(inboxHeadProbeSQL, inboxPendingPredicate) {
		t.Fatalf("队头复核必须带同一个时间窗谓词: %s", inboxHeadProbeSQL)
	}
}
