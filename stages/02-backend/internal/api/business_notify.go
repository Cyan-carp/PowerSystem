package api

import (
	"bytes"
	"context"
	"fmt"
	"net/http"
	"time"

	"go.uber.org/zap"
	"gorm.io/gorm"
)

type pendingBusinessNotification struct {
	ID       int64
	Payload  string
	Attempts int
}

func retryNotificationAfter(attempts int) time.Duration {
	if attempts > 7 {
		attempts = 7
	}
	if attempts < 1 {
		attempts = 1
	}
	return time.Duration(1<<attempts) * time.Second
}

func (s *Server) businessNotificationWorker(ctx context.Context) {
	ticker := time.NewTicker(2 * time.Second)
	defer ticker.Stop()
	for {
		if err := s.deliverBusinessNotification(ctx); err != nil && ctx.Err() == nil {
			s.log.Warn("business_notification_worker_failed", zap.Error(err))
		}
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}

func (s *Server) deliverBusinessNotification(ctx context.Context) error {
	return s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		var item pendingBusinessNotification
		if err := tx.Raw(`SELECT id,payload,attempts FROM business_notification_outbox
			WHERE sent_at IS NULL AND next_attempt_at <= now()
			ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED`).Scan(&item).Error; err != nil {
			return err
		}
		if item.ID == 0 {
			return nil
		}
		requestCtx, cancel := context.WithTimeout(ctx, 8*time.Second)
		defer cancel()
		request, err := http.NewRequestWithContext(requestCtx, http.MethodPost, s.cfg.BusinessNotifyURL, bytes.NewBufferString(item.Payload))
		if err == nil {
			request.Header.Set("Content-Type", "application/json")
			request.Header.Set("X-PowerSystem-Token", s.cfg.BusinessNotifyToken)
			var response *http.Response
			response, err = (&http.Client{Timeout: 8 * time.Second}).Do(request)
			if err == nil {
				response.Body.Close()
				if response.StatusCode != http.StatusOK {
					err = fmt.Errorf("adapter HTTP %d", response.StatusCode)
				}
			}
		}
		if err != nil {
			if updateErr := tx.Exec(`UPDATE business_notification_outbox
				SET attempts=attempts+1,next_attempt_at=? WHERE id=?`,
				time.Now().Add(retryNotificationAfter(item.Attempts+1)), item.ID).Error; updateErr != nil {
				return updateErr
			}
			s.log.Warn("business_notification_retry", zap.Int64("outbox_id", item.ID), zap.Int("attempt", item.Attempts+1))
			return nil
		}
		return tx.Exec("UPDATE business_notification_outbox SET sent_at=now() WHERE id=?", item.ID).Error
	})
}
