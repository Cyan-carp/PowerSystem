package service

import (
	"errors"
	"gorm.io/gorm"
	"powersystem/backend/internal/model"
	"time"
)

var ErrAlarmAckConflict = errors.New("alarm already acknowledged or recovered")

// Shared by the authenticated web action and server-local demo command.
// Caller owns the transaction and actor identity; notification stays atomic.
func AcknowledgeAlarm(tx *gorm.DB, id, actor int64, now time.Time) (model.AlarmRecord, error) {
	var alarm model.AlarmRecord
	r := tx.Model(&model.AlarmRecord{}).Where("id=? AND status='unhandled' AND recovered_at IS NULL", id).Updates(map[string]any{"status": "acked", "acked_by": actor, "acked_at": now, "updated_at": now})
	if r.Error != nil {
		return alarm, r.Error
	}
	if r.RowsAffected == 0 {
		return alarm, ErrAlarmAckConflict
	}
	if err := tx.First(&alarm, id).Error; err != nil {
		return alarm, err
	}
	var device model.Device
	if err := tx.First(&device, alarm.DeviceID).Error; err != nil {
		return alarm, err
	}
	return alarm, QueueAlarmNotification(tx, device, alarm, "acknowledged")
}
