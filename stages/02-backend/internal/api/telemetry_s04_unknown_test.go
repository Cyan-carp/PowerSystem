//go:build tr_s04 && tr_s02 && windows

package api

import (
	"context"
	"database/sql"
	"fmt"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"powersystem/backend/internal/model"
	"powersystem/backend/internal/service"
	"testing"
	"time"
)

func TestTRS04UnknownCommit(t *testing.T) {
	f := s04new(t)
	sql.Register("s04-unknown", &s02Driver{})
	wrapped, e := sql.Open("s04-unknown", s04DSN)
	s04must(t, e)
	defer wrapped.Close()
	wdb, e := gorm.Open(postgres.New(postgres.Config{Conn: wrapped}), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	s04must(t, e)
	for _, mode := range []int32{1, 2} {
		d := f.device(int(mode))
		s04must(t, f.db.Create(&model.AlarmRule{DeviceID: d.ID, Metric: "temperature", Operator: ">", Threshold: 50, Level: "major", Enabled: true, Version: "1"}).Error)
		i := f.item(d, fmt.Sprintf("unknown-%d", mode), 1, time.Now().UnixMilli())
		f.s.db = wdb
		unknownMode.Store(mode)
		f.s.processInboxItem(context.Background(), i)
		f.s.db = f.db
		if mode == 1 && s04state(f, i.ID) != "success" {
			t.Fatal("committed outcome lost")
		}
		if mode == 2 && s04state(f, i.ID) != "pending" {
			t.Fatal("rolled-back outcome invented")
		}
		if _, e := service.CompensateTelemetry(f.db, i.ID, fmt.Sprint(mode), "fixture", "default inspect unknown result", false); e != nil {
			t.Fatal(e)
		}
		f.s.processInboxItem(context.Background(), i)
		f.s.processInboxItem(context.Background(), i)
		if s04state(f, i.ID) != "success" || s04row(f, i.ID).Attempts != 0 {
			t.Fatal("unknown replay classified as row failure")
		}
	}
	var alarms, outbox int64
	f.db.Table("alarm_records").Count(&alarms)
	f.db.Table("business_notification_outbox").Count(&outbox)
	if alarms != 2 || outbox != 2 {
		t.Fatal("unknown commit duplicated effects", alarms, outbox)
	}
	t.Log("real PG COMMIT applied/lost-reply and rolled-back/lost-reply; retry lock-reads terminal result before TD/business; two alarms/outbox exactly, default compensation inspection unchanged; no external dispatcher")
}
