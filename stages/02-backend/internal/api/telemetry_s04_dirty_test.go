//go:build tr_s04 && windows

package api

import (
	"context"
	"encoding/json"
	"powersystem/backend/internal/energy"
	"powersystem/backend/internal/model"
	"powersystem/backend/internal/telemetry"
	"testing"
	"time"
)

func TestTRS04DirtyProvenance(t *testing.T) {
	f := s04new(t)
	ctx := context.Background()
	d := f.device(1)
	now := time.Now()
	a := f.item(d, "dirty-base", 1, now.Add(-10*time.Second).UnixMilli())
	b := f.item(d, "dirty-base", 2, now.Add(-5*time.Second).UnixMilli())
	f.s.processInboxItem(ctx, a)
	f.s.processInboxItem(ctx, b)
	if _, e := energy.RebuildPending(ctx, f.db, 12); e != nil {
		t.Fatal(e)
	}
	day := now.In(energyReportZone).Format(energyReportDayLayout)
	rows, e := f.s.energyReportRows([]model.Device{d}, day, day)
	s04must(t, e)
	if len(rows) != 1 || rows[0].SourceQuality != "verified" || rows[0].DirtyPending {
		t.Fatal("verified base", rows)
	}
	incoming := f.sample(d, "dirty-next", 1, time.Now().UnixMilli())
	s04must(t, f.s.td.Insert(ctx, d, []telemetry.Sample{incoming}))
	payload, _ := json.Marshal(incoming)
	if !f.s.receive(nil, s04Message{payload}) {
		t.Fatal("durable reception failed")
	}
	s04must(t, f.s.refreshDeviceSnapshot(ctx, d))
	rows, e = f.s.energyReportRows([]model.Device{d}, day, day)
	s04must(t, e)
	if len(rows) != 1 || !rows[0].DirtyPending || rows[0].SourceQuality != "verified" {
		t.Fatal("dirty/source separation", rows)
	}
	summary, e := energy.ReadSummary(ctx, f.db, time.Now())
	s04must(t, e)
	if !summary.TodayProvisional || !summary.RetainedProvisional {
		t.Fatal("summary hid pending rebuild")
	}
	report := buildEnergyReport(scopeSelection{Scope: "all"}, d.ID, []model.Device{d}, rows, []string{day}, now.AddDate(0, 0, 1), time.Now())
	if report.Complete || report.Daily[0].CoverageComplete {
		t.Fatal("dirty fabricated completeness")
	}
	f.s.processPending(ctx)
	if _, e := energy.RebuildPending(ctx, f.db, 12); e != nil {
		t.Fatal(e)
	}
	rows, e = f.s.energyReportRows([]model.Device{d}, day, day)
	s04must(t, e)
	if rows[0].DirtyPending || rows[0].SourceQuality != "verified" {
		t.Fatal("rebuild did not settle")
	}
	t.Log("real PG/TD: incoming TD-confirmed pending keeps previous verified aggregate provisional via dirty_pending; source quality retained; committed row/dirty-version rebuild clears marker without synthesizing energy or alarms")
}
