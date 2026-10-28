package service

import (
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"gorm.io/gorm"
	"powersystem/backend/internal/energy"
	"powersystem/backend/internal/model"
)

// Compensation is deliberately unavailable to automatic workers and HTTP.
// Default is a locked inspection. Mutation is limited to the dedicated S03 DB.
// This does not authorize a production replay or external notification.
func CompensateTelemetry(db *gorm.DB, id int64, request, actor, reason string, isolatedReplay bool) (string, error) {
	outcome := "inspect"
	if id <= 0 || len(request) > 128 || strings.TrimSpace(request) == "" || strings.TrimSpace(actor) == "" || strings.TrimSpace(reason) == "" {
		return "", errors.New("compensation_identity_required")
	}
	err := TelemetryTransaction(db, func(tx *gorm.DB) error {
		var identity struct{ DeviceID int64 }
		if err := tx.Raw("SELECT device_id FROM telemetry_inbox WHERE id=?", id).Scan(&identity).Error; err != nil {
			return err
		}
		if err := TelemetryDeviceLock(tx, identity.DeviceID); err != nil {
			return err
		}
		var item model.Inbox
		if err := tx.Raw(`SELECT * FROM telemetry_inbox WHERE id=? FOR UPDATE`, id).Scan(&item).Error; err != nil {
			return err
		}
		if item.ID == 0 {
			return errors.New("inbox_missing")
		}
		// Always resolve durable result first, including an indeterminate commit.
		if !isolatedReplay {
			if item.BusinessState != nil {
				outcome = *item.BusinessState
			}
			return nil
		}
		var environment struct {
			DB      string
			Address string
			Port    int
		}
		if err := tx.Raw(`SELECT current_database() AS db,host(inet_server_addr()) AS address,inet_server_port() AS port`).Scan(&environment).Error; err != nil {
			return err
		}
		if environment.DB != "tr_s03_test" || environment.Address != "127.0.0.1" || environment.Port != 15441 {
			return errors.New("compensation_isolation_required")
		}
		var prior struct {
			InboxID                int64
			Actor, Reason, Outcome string
		}
		r := tx.Raw(`SELECT inbox_id,actor,reason,outcome FROM telemetry_compensations WHERE request_id=?`, request).Scan(&prior)
		if r.Error != nil {
			return r.Error
		}
		if r.RowsAffected > 0 {
			if prior.InboxID != id || prior.Actor != actor || prior.Reason != reason {
				return errors.New("compensation_request_conflict")
			}
			outcome = prior.Outcome
			return nil
		}
		before, err := json.Marshal(item)
		if err != nil {
			return err
		}
		outcome = "terminal_unchanged"
		if item.BusinessState != nil && *item.BusinessState == "quarantined" {
			var later int64
			if err := tx.Raw(`SELECT id FROM telemetry_inbox WHERE device_id=? AND id>? AND processed_at IS NOT NULL ORDER BY id LIMIT 1`, item.DeviceID, item.ID).Scan(&later).Error; err != nil {
				return err
			}
			if later != 0 {
				return errors.New("compensation_successor_already_terminal")
			}
			// A reviewed new attempt gets a new continuous-evidence window.
			// Lifetime attempts/first failure remain on the row; prior_state also
			// retains the original reason and complete quarantine evidence.
			if err := tx.Model(&model.Inbox{}).Where("id=?", id).Updates(map[string]any{
				"business_state": "retry", "processed_at": nil, "next_attempt_at": time.Now().UTC(),
				"failure_code": nil, "reason_since": nil, "reason_attempts": 0,
				"missing_since": nil, "missing_checked_at": nil, "missing_attempts": 0,
			}).Error; err != nil {
				return err
			}
			if err := energy.MarkDirty(tx, item.DeviceID, item.TS); err != nil {
				return err
			}
			outcome = "isolated_requeued"
		}
		return tx.Exec(`INSERT INTO telemetry_compensations(request_id,inbox_id,actor,reason,prior_state,outcome) VALUES(?,?,?,?,?::jsonb,?)`, request, id, actor, reason, string(before), outcome).Error
	})
	return outcome, err
}

// One namespaced PostgreSQL transaction lock also fences manual requeue against
// a successor that has already passed its durable head check.
func TelemetryDeviceLock(tx *gorm.DB, deviceID int64) error {
	if tx.Dialector.Name() != "postgres" {
		return nil
	}
	return tx.Exec("SELECT pg_advisory_xact_lock(hashtextextended(?,0))", fmt.Sprintf("telemetry-device:%d", deviceID)).Error
}
