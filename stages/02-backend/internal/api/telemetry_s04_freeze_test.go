//go:build tr_s04 && windows

package api

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"powersystem/backend/internal/tdengine"
	"testing"
	"time"
)

func TestTRS04RealtimeDrain(t *testing.T) {
	f := s04new(t)
	ctx := context.Background()
	d := f.device(1)
	first := f.item(d, "drain", 1, time.Now().UnixMilli())
	s04must(t, f.s.refreshDeviceSnapshot(ctx, d))
	second := f.item(d, "drain", 2, time.Now().UnixMilli()+1)
	s04must(t, f.rd.Do(ctx, "CLIENT", "PAUSE", 600, "WRITE").Err())
	done := make(chan error, 1)
	go func() { done <- f.s.refreshDeviceSnapshot(ctx, d) }()
	s04wait(t, time.Second, func() bool {
		var n int64
		f.db.Raw(`SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND state='idle in transaction' AND query LIKE '%JOIN telemetry_inbox i ON i.id=s.inbox_id%'`).Scan(&n)
		return n > 0
	})
	freezeStarted := time.Now()
	frozen := make(chan error, 1)
	go func() { frozen <- f.db.Exec("UPDATE alarm_maintenance_state SET enabled=true WHERE id=1").Error }()
	select {
	case <-frozen:
		t.Fatal("freeze crossed in-flight Redis write")
	case <-time.After(80 * time.Millisecond):
	}
	s04must(t, <-done)
	s04must(t, <-frozen)
	drain := time.Since(freezeStarted)
	if e := f.s.refreshDeviceSnapshot(ctx, d); e == nil {
		t.Fatal("post-barrier snapshot admitted")
	}
	f.s.processInboxItem(ctx, first)
	if s04state(f, first.ID) != "pending" {
		t.Fatal("post-barrier business admitted")
	}
	s04must(t, f.db.Exec("UPDATE alarm_maintenance_state SET enabled=false WHERE id=1").Error)
	third := f.item(d, "drain", 3, time.Now().UnixMilli())
	entered := make(chan struct{}, 1)
	release := make(chan struct{})
	proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		select {
		case entered <- struct{}{}:
		default:
		}
		select {
		case <-r.Context().Done():
		case <-release:
		}
	}))
	defer proxy.Close()
	defer close(release)
	cfg := f.cfg
	cfg.TDURL = proxy.URL
	original := f.s.td
	f.s.td = tdengine.New(cfg)
	cancelled, cancel := context.WithCancel(ctx)
	cancelledDone := make(chan error, 1)
	go func() { cancelledDone <- f.s.refreshDeviceSnapshot(cancelled, d) }()
	<-entered
	cancel()
	if e := <-cancelledDone; e == nil {
		t.Fatal("inflight cancellation hidden")
	}
	f.s.td = original
	if s04row(f, third.ID).TDConfirmedAt != nil {
		t.Fatal("cancelled verification committed")
	}
	var pointer int64
	f.db.Raw("SELECT inbox_id FROM telemetry_snapshots WHERE device_id=?", d.ID).Scan(&pointer)
	if pointer != second.ID {
		t.Fatal("cancelled verification moved source")
	}
	s04must(t, f.db.Exec("ALTER TABLE alarm_maintenance_state RENAME TO s04_gate_hidden").Error)
	if e := f.s.refreshDeviceSnapshot(ctx, d); e == nil {
		t.Fatal("SQL-error gate admitted")
	}
	s04must(t, f.db.Exec("ALTER TABLE s04_gate_hidden RENAME TO alarm_maintenance_state").Error)
	t.Logf("actual Redis WRITE pause holds real PG SHARE gate; freeze drains %s, both channels fenced after commit; canceled TD protocol request creates no durable confirmation; SQL-error gate closed (%s)", drain, fmt.Sprint(third.ID))
}
