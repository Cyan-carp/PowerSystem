package api

import (
	"context"
	"encoding/json"
	"time"

	"powersystem/backend/internal/model"
	"powersystem/backend/internal/service"
)

// Local commands publish an ID/type hint after the same business transaction.
// The API reloads the committed manual record and uses its normal WS channel;
// caller-provided alarm bodies can never become display or model evidence.
func (s *Server) manualAlarmEvents(ctx context.Context) {
	for ctx.Err() == nil {
		sub := s.redis.Subscribe(ctx, service.ManualAlarmEventsChannel)
		if _, err := sub.Receive(ctx); err == nil {
			messages := sub.Channel()
		consume:
			for {
				select {
				case <-ctx.Done():
					sub.Close()
					return
				case msg, ok := <-messages:
					if !ok {
						break consume
					}
					s.receiveManualAlarmEvent(ctx, []byte(msg.Payload))
				}
			}
		}
		sub.Close()
		select {
		case <-ctx.Done():
			return
		case <-time.After(time.Second):
		}
	}
}

func (s *Server) receiveManualAlarmEvent(parent context.Context, raw []byte) {
	if len(raw) > 1024 {
		return
	}
	var signal struct {
		ID   int64  `json:"alarm_id"`
		Type string `json:"type"`
	}
	if json.Unmarshal(raw, &signal) != nil || signal.ID <= 0 {
		return
	}
	status, ok := map[string]string{"alarm_created": "unhandled", "alarm_acked": "acked", "alarm_recovered": "recovered"}[signal.Type]
	if !ok {
		return
	}
	ctx, cancel := context.WithTimeout(parent, inboxTransactionBudget)
	defer cancel()
	if s.alarmMaintenance(ctx) {
		return
	}
	var alarm model.AlarmRecord
	if s.db.WithContext(ctx).First(&alarm, signal.ID).Error != nil || !service.IsManualDemo(alarm) || alarm.Status != status {
		return
	}
	s.broadcast(service.AlarmEvent{Type: signal.Type, Data: alarm})
}
