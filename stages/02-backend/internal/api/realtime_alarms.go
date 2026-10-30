package api

import (
	"context"
	"time"

	"gorm.io/gorm"
	"powersystem/backend/internal/model"
	"powersystem/backend/internal/service"
	"powersystem/backend/internal/telemetry"
)

// The latest exact TD-confirmed source may drive current alarms even while an
// older receipt waits for missing TD data. Historical order and gap evidence
// remain untouched; notifications and the watermark commit atomically.
func (s *Server) evaluateRealtimeSource(ctx context.Context, device model.Device, expectedID int64) error {
	var events []service.AlarmEvent
	err := service.TelemetryTransaction(s.db.WithContext(ctx), func(tx *gorm.DB) error {
		if err := service.TelemetryDeviceLock(tx, device.ID); err != nil {
			return err
		}
		var item model.Inbox
		r := tx.Raw(`SELECT i.* FROM telemetry_snapshots s JOIN telemetry_inbox i ON i.id=s.inbox_id
 JOIN devices d ON d.id=i.device_id AND d.deleted_at IS NULL AND d.device_code=? AND d.station_code=?
 WHERE s.device_id=? AND s.inbox_id=? AND i.device_id=? AND i.td_confirmed_at IS NOT NULL FOR UPDATE OF i`, device.DeviceCode, device.StationCode, device.ID, expectedID, device.ID).Scan(&item)
		if r.Error != nil {
			return r.Error
		}
		if item.ID == 0 || item.ProcessedAt != nil || item.AlarmEvaluation != nil {
			return nil
		}
		sample, valid := validSnapshotItem(device, item)
		if !valid || telemetry.Freshness(time.Now().UTC(), item.ReceivedAt, sample.TS) != "fresh" {
			return nil
		}
		var err error
		events, err = service.EvaluateRealtimeAlarms(tx, item, sample, device)
		return err
	})
	if err != nil {
		return err
	}
	for _, event := range events {
		s.broadcast(event)
	}
	return nil
}
