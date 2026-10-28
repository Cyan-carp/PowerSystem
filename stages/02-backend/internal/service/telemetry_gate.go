package service

import (
	"database/sql"
	"errors"
	"fmt"
	"gorm.io/gorm"
)

var ErrAlarmMaintenance = errors.New("alarm_maintenance_enabled")
var ErrMaintenanceMissing = errors.New("alarm_maintenance_missing")
var ErrCommitUnknown = errors.New("telemetry_commit_unknown")
var ErrInboxTerminal = errors.New("inbox_already_terminal")

// The existing UPDATE enabled=true is the freeze barrier: it waits for all
// admitted transactions' SHARE locks. Never cache a disabled gate. SQLite is
// used only by unit fixtures; production is PostgreSQL.
func AlarmWriteGate(tx *gorm.DB) error {
	if tx.Dialector.Name() != "postgres" {
		return nil
	}
	var state struct{ Enabled bool }
	r := tx.Raw("SELECT enabled FROM alarm_maintenance_state WHERE id=1 FOR SHARE").Scan(&state)
	if r.Error != nil {
		return r.Error
	}
	if r.RowsAffected != 1 {
		return ErrMaintenanceMissing
	}
	if state.Enabled {
		return ErrAlarmMaintenance
	}
	return nil
}

// A failed COMMIT acknowledgement is indeterminate. The next attempt must
// lock/read the durable inbox before invoking any business operation.
func TelemetryTransaction(db *gorm.DB, fn func(*gorm.DB) error) error {
	tx := db.Begin()
	if tx.Error != nil {
		return tx.Error
	}
	defer tx.Rollback()
	if err := AlarmWriteGate(tx); err != nil {
		return err
	}
	if err := fn(tx); err != nil {
		return err
	}
	if err := tx.Statement.Context.Err(); err != nil {
		return err
	}
	if err := tx.Commit().Error; err != nil {
		if errors.Is(err, sql.ErrTxDone) && tx.Statement.Context.Err() != nil {
			return tx.Statement.Context.Err()
		}
		return fmt.Errorf("%w: %v", ErrCommitUnknown, err)
	}
	return nil
}
