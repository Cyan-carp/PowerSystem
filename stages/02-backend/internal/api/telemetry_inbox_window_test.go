package api

import (
	"strings"
	"testing"
)

// 取任务与事务内队头复核共用同一谓词、同一方向。2026-11-03 两处漂移过一次：
// 取任务改成最新优先而复核没跟，每条样本都被判「已不是队头」，走 errInboxDeferred
// 静默返回，入库成功率归零 40 分钟。这条测试只盯这个回归，不连数据库。
func TestInboxPredicateSharedByHeadsAndProbe(t *testing.T) {
	if !strings.Contains(inboxHeadProbeSQL, inboxPendingPredicate) {
		t.Fatalf("队头复核未使用共享谓词: %s", inboxHeadProbeSQL)
	}
	if !strings.Contains(inboxHeadProbeSQL, "ORDER BY id ASC") {
		t.Fatalf("复核方向必须与取任务一致（ASC 先进先出）: %s", inboxHeadProbeSQL)
	}
	if got := strings.Count(inboxHeadProbeSQL, "?"); got != 2 {
		t.Fatalf("复核占位符应为 device_id + 时间窗共 2 个，实际 %d: %s", got, inboxHeadProbeSQL)
	}
}

// 两段取任务（游标之后与本轮回绕）都要带时间窗，且参数个数、顺序与占位符对齐。
// 参数顺序写错同样会造成「查不到队头」，只是表现为全部 deferred，很难从日志看出。
func TestInboxHeadsQueryWrapsWithWindow(t *testing.T) {
	const (
		after = int64(311)
		floor = int64(1793721600000)
		limit = 500
	)
	for _, through := range []int64{0, 200} {
		q, args := buildInboxHeadsQuery(after, floor, through, limit)
		if got := strings.Count(q, inboxPendingPredicate); got != 2 {
			t.Fatalf("through=%d: 两段都必须带时间窗，实际 %d 处: %s", through, got, q)
		}
		if strings.Contains(q, "id DESC") {
			t.Fatalf("through=%d: 仍在按最新优先取任务: %s", through, q)
		}
		if got, want := strings.Count(q, "?"), len(args); got != want {
			t.Fatalf("through=%d: 占位符 %d 个，参数 %d 个: %v", through, got, want, args)
		}
		if args[0] != floor {
			t.Fatalf("through=%d: 首段下界应为 %d，实际 %v", through, floor, args)
		}
		if args[len(args)-1] != limit {
			t.Fatalf("through=%d: 末位应为 limit，实际 %v", through, args)
		}
		if through > 0 {
			if args[2] != through || args[len(args)-2] != through {
				t.Fatalf("through=%d: 两段上界参数位置不对: %v", through, args)
			}
		}
	}
}

// 窗口下界为 0 时 ts_ms>=0 恒真，等价于不设窗。这是让 30 多个直接调用
// processInboxItem 的既有测试保持原语义的约定，不能改成 NULL 或跳过。
func TestInboxWindowZeroMeansNoFilter(t *testing.T) {
	q, args := buildInboxHeadsQuery(0, 0, 0, 10)
	if !strings.Contains(q, inboxPendingPredicate) {
		t.Fatalf("谓词必须始终存在: %s", q)
	}
	if args[0] != int64(0) {
		t.Fatalf("下界应为 0，实际 %v", args)
	}
}
