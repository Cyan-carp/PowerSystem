package service

import (
	"fmt"
	"sync"
	"testing"
	"time"

	"gorm.io/gorm"
	"powersystem/backend/internal/model"
)

// The dedicated driver first seeds the complete catalog with the existing
// two-day window fixture, then runs this case in that same disposable database.
func TestManualDemoLifecycleAndDailyQuota(t *testing.T) {
	db := alarmRepairDB(t)
	must := func(e error) {
		t.Helper()
		if e != nil {
			t.Fatal(e)
		}
	}
	actor := model.User{Username: "demo-fixture-operator", PasswordHash: "not-a-login", Role: "operator"}
	must(db.Create(&actor).Error)
	var group sync.WaitGroup
	ids := make(chan int64, 6)
	for n := 0; n < 6; n++ {
		group.Add(1)
		go func() {
			defer group.Done()
			a, _, e := CreateManualAPIDemo(db, "INV-1001", "same-request-fixture", "major")
			if e != nil {
				t.Error(e)
				return
			}
			ids <- a.ID
		}()
	}
	group.Wait()
	close(ids)
	var id int64
	for got := range ids {
		if id != 0 && id != got {
			t.Fatal("concurrent retry duplicated alarm")
		}
		id = got
	}
	if id == 0 {
		t.Fatal("no alarm")
	}
	if _, _, e := CreateManualAPIDemo(db, "INV-1001", "another-active-key", "major"); e == nil {
		t.Fatal("second active alarm bypassed lifecycle")
	}
	var taskCount int64
	must(db.Table("agent_interpretations").Where("event_key=?", ManualAPIDemoEventPrefix+"same-request-fixture").Count(&taskCount).Error)
	if taskCount != 1 {
		t.Fatal("duplicate interpretation")
	}
	if _, _, e := CreateManualAPIDemo(db, "INV-1001", "same-request-fixture", "urgent"); e == nil {
		t.Fatal("idempotency key accepted changed options")
	}
	if _, _, e := AckManualAPIDemo(db, id, "missing-actor"); e == nil {
		t.Fatal("invented actor allowed")
	}
	a, changed, e := AckManualAPIDemo(db, id, actor.Username)
	must(e)
	if !changed || a.Status != "acked" || a.AckedBy == nil || *a.AckedBy != actor.ID {
		t.Fatal("acknowledgement audit missing")
	}
	_, changed, e = AckManualAPIDemo(db, id, actor.Username)
	must(e)
	if changed {
		t.Fatal("duplicate ack")
	}
	a, changed, e = EndManualAPIDemo(db, id)
	must(e)
	if !changed || a.Status != "recovered" || a.RecoveredAt == nil {
		t.Fatal("recovery failed")
	}
	_, changed, e = EndManualAPIDemo(db, id)
	must(e)
	if changed {
		t.Fatal("duplicate recovery")
	}
	var events int64
	must(db.Table("business_notification_outbox").Where("event_key IN ?", []string{fmtAlarmKey(id, "triggered"), fmtAlarmKey(id, "acknowledged"), fmtAlarmKey(id, "recovered")}).Count(&events).Error)
	if events != 3 {
		t.Fatal("business outbox not exactly once", events)
	}
	var wrong model.AlarmRecord
	must(db.Where("origin='normal'").First(&wrong).Error)
	if _, _, e = EndManualAPIDemo(db, wrong.ID); e == nil {
		t.Fatal("manual command changed a regular alarm")
	}
	// Six manual demos on a new isolated day must leave all five natural slots.
	clock := time.Date(2026, 11, 1, 9, 1, 0, 0, SimulationLocation())
	for n := 0; n < 6; n++ {
		analysis := "model"
		if n%2 == 0 {
			analysis = "fixed"
		}
		a, _, e := CreateManualDemo(db, "INV-1001", fmt.Sprintf("quota-fixture-%02d", n), "minor", analysis)
		must(e)
		must(db.Model(&a).Update("triggered_at", clock).Error)
		_, _, e = EndManualAPIDemo(db, a.ID)
		must(e)
	}
	must(TelemetryTransaction(db, func(tx *gorm.DB) error {
		selected, e := selectedFleetDevice(tx, "PV-01", clock)
		if e != nil {
			return e
		}
		var device model.Device
		if e = tx.First(&device, selected).Error; e != nil {
			return e
		}
		allowed, e := permitFleetNaturalAlarm(tx, device, "temperature", clock, clock)
		if e == nil && !allowed {
			t.Error("manual demos consumed the five daily natural slots")
		}
		return e
	}))
	t.Log("same business notification transaction: concurrent create dedup, actor audit, acknowledgement/recovery idempotency, non-demo protection and daily quota exclusion")
}

func fmtAlarmKey(id int64, state string) string { return fmt.Sprintf("alarm:%d:%s", id, state) }
