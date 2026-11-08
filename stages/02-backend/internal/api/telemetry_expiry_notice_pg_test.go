//go:build ir_s02

package api

import (
	"context"
	"encoding/json"
	"fmt"
	"testing"
	"time"
)

func TestIRS02ExpiryNoticeAtomicAndBounded(t *testing.T) {
	t.Run("one_notice_per_quarter_hour", func(t *testing.T) {
		db, s := irS02Fixture(t)
		d := irS02Device(t, db, "IR-S02-NOTICE")
		now := time.Now().UTC()
		old := now.Add(-8 * 24 * time.Hour)
		first := irS02Insert(t, db, d, 1, old.UnixMilli(), old, irS02Options{})
		floor := now.Add(-inboxWindow).UnixMilli()
		changed, err := s.expireInboxBatch(context.Background(), floor, 10)
		if err != nil || changed != 1 {
			t.Fatalf("first expiry: changed=%d err=%v", changed, err)
		}
		var notice struct {
			EventKey string
			Payload  []byte
		}
		if err := db.Raw("SELECT event_key,payload FROM business_notification_outbox").Scan(&notice).Error; err != nil {
			t.Fatal(err)
		}
		var payload struct {
			EventID      string `json:"event_id"`
			Category     string `json:"category"`
			State        string `json:"state"`
			Rows         int64  `json:"rows"`
			FirstInboxID int64  `json:"first_inbox_id"`
			LastInboxID  int64  `json:"last_inbox_id"`
			OccurredAt   string `json:"occurred_at"`
			TestOnly     bool   `json:"test_only"`
		}
		if err := json.Unmarshal(notice.Payload, &payload); err != nil {
			t.Fatal(err)
		}
		at, err := time.Parse(time.RFC3339, payload.OccurredAt)
		if err != nil {
			t.Fatal(err)
		}
		wantKey := fmt.Sprintf("inbox-expiry:%s", at.UTC().Truncate(15*time.Minute).Format("200601021504"))
		if notice.EventKey != wantKey || payload.EventID != wantKey || payload.Category != "inbox_expiry" ||
			payload.State != "quarantined" || payload.Rows != 1 || payload.FirstInboxID != first || payload.LastInboxID != first || payload.TestOnly {
			t.Fatalf("wrong durable notice: key=%s payload=%+v", notice.EventKey, payload)
		}

		second := irS02Insert(t, db, d, 2, old.Add(time.Second).UnixMilli(), old.Add(time.Second), irS02Options{})
		changed, err = s.expireInboxBatch(context.Background(), floor, 10)
		if err != nil || changed != 1 || !irS02Terminal(irS02Load(t, db, second)) {
			t.Fatalf("second expiry: changed=%d err=%v", changed, err)
		}
		var count int64
		if err := db.Raw("SELECT count(*) FROM business_notification_outbox").Scan(&count).Error; err != nil || count != 1 {
			t.Fatalf("notice count=%d err=%v", count, err)
		}
		probe := irS02Insert(t, db, d, 3, old.Add(2*time.Second).UnixMilli(), old.Add(2*time.Second), irS02Options{})
		if err := db.Exec("UPDATE telemetry_inbox SET run_id='ir_s02_probe_fixture' WHERE id=?", probe).Error; err != nil {
			t.Fatal(err)
		}
		changed, err = s.expireInboxBatch(context.Background(), floor, 10)
		if err != nil || changed != 1 {
			t.Fatalf("probe expiry: changed=%d err=%v", changed, err)
		}
		var probeNotice struct {
			EventKey string
			Payload  []byte
		}
		if err := db.Raw("SELECT event_key,payload FROM business_notification_outbox WHERE event_key LIKE 'inbox-expiry-test:%'").Scan(&probeNotice).Error; err != nil {
			t.Fatal(err)
		}
		var probePayload struct {
			TestOnly bool `json:"test_only"`
		}
		if err := json.Unmarshal(probeNotice.Payload, &probePayload); err != nil || !probePayload.TestOnly || probeNotice.EventKey == "" {
			t.Fatalf("probe notification not separate: key=%s payload=%s err=%v", probeNotice.EventKey, probeNotice.Payload, err)
		}
		if err := db.Raw("SELECT count(*) FROM business_notification_outbox").Scan(&count).Error; err != nil || count != 2 {
			t.Fatalf("separate normal and probe notices: count=%d err=%v", count, err)
		}
	})

	t.Run("notice_failure_rolls_back_expiry", func(t *testing.T) {
		db, s := irS02Fixture(t)
		d := irS02Device(t, db, "IR-S02-NOTICE-FAIL")
		now := time.Now().UTC()
		old := now.Add(-8 * 24 * time.Hour)
		id := irS02Insert(t, db, d, 1, old.UnixMilli(), old, irS02Options{})
		if err := db.Exec("DROP TABLE business_notification_outbox").Error; err != nil {
			t.Fatal(err)
		}
		if changed, err := s.expireInboxBatch(context.Background(), now.Add(-inboxWindow).UnixMilli(), 10); err == nil || changed != 0 {
			t.Fatalf("expiry must fail closed with missing outbox: changed=%d err=%v", changed, err)
		}
		if row := irS02Load(t, db, id); row.ProcessedAt != nil || row.BusinessState == nil || *row.BusinessState != "pending" {
			t.Fatalf("expiry committed without durable notice: %+v", row)
		}
	})
}
