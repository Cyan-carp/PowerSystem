//go:build tr_s04 && windows

package api

import (
	"context"
	"fmt"
	"powersystem/backend/internal/model"
	"testing"
	"time"
)

func TestTRS04Fairness(t *testing.T) {
	t.Run("501_tail_wrap", func(t *testing.T) {
		f := s04new(t)
		var high model.Inbox
		for n := 0; n < 501; n++ {
			d := f.device(n)
			i := f.item(d, "fair", 1, time.Now().UnixMilli())
			if n < 500 {
				s04must(t, f.db.Exec("UPDATE telemetry_inbox SET business_state='retry',next_attempt_at=now()+interval '1 hour' WHERE id=?", i.ID).Error)
			} else {
				high = i
			}
		}
		start := time.Now()
		f.s.processPending(context.Background())
		f.s.processPending(context.Background())
		if s04state(f, high.ID) != "success" || time.Since(start) > 10*time.Second {
			t.Fatal("high-ID starvation")
		}
		var retained int64
		f.db.Model(&model.Inbox{}).Where("processed_at IS NULL").Count(&retained)
		if retained != 500 {
			t.Fatal("deferred heads lost", retained)
		}
		t.Logf("501st device processed in %s across tail/wrap; all 500 deferred heads retained", time.Since(start))
	})
	t.Run("143_budget_resume", func(t *testing.T) {
		f := s04new(t)
		var slowIDs []int64
		var fastIDs []int64
		for n := 0; n < 143; n++ {
			d := f.device(n)
			i := f.item(d, "budget", 1, time.Now().UnixMilli())
			if n < 5 {
				slowIDs = append(slowIDs, i.ID)
			} else {
				fastIDs = append(fastIDs, i.ID)
			}
		}
		s04must(t, f.db.Exec(fmt.Sprintf(`CREATE FUNCTION s04_slow() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.id<=%d AND NEW.td_confirmed_at IS NOT NULL THEN PERFORM pg_sleep(0.3); END IF; RETURN NEW; END $$; CREATE TRIGGER s04_slow BEFORE UPDATE ON telemetry_inbox FOR EACH ROW EXECUTE FUNCTION s04_slow()`, slowIDs[4])).Error)
		start := time.Now()
		rounds := 0
		for ; time.Since(start) < 20*time.Second; rounds++ {
			f.s.processPendingBudget(context.Background(), 120*time.Millisecond)
			var complete int64
			f.db.Model(&model.Inbox{}).Where("id IN ? AND business_state='success'", fastIDs).Count(&complete)
			if complete == 138 {
				break
			}
		}
		var complete int64
		f.db.Model(&model.Inbox{}).Where("id IN ? AND business_state='success'", fastIDs).Count(&complete)
		if complete != 138 {
			t.Fatal("budget starved other devices", complete)
		}
		for _, id := range slowIDs {
			if s04state(f, id) != "pending" {
				t.Fatal("cancelled slow row terminalized")
			}
		}
		t.Logf("143 devices / five 300ms slow heads / 120ms rounds: 138 others committed in %s/%d rounds; slow heads retained and cancellation not quarantine", time.Since(start), rounds+1)
	})
}
