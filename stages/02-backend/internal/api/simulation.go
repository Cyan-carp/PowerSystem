package api

import (
	"context"
	"time"

	"go.uber.org/zap"

	"powersystem/backend/internal/service"
)

const simulationTickInterval = 15 * time.Second

// Legacy controlled configuration now drains old demonstrations only. It
// must never create fresh synthetic alarm records after natural rules enable.
func normalizeSimulationAlarmMode(raw string) string {
	switch raw {
	case service.SimulationModeControlled:
		return service.SimulationModePaused
	case service.SimulationModePaused:
		return service.SimulationModePaused
	default:
		return service.SimulationModeOff
	}
}

// simulationAlarmWorker only closes legacy demonstrations; controlled is a
// deprecated input accepted for compatibility, not a fallback mode.
func (s *Server) simulationAlarmWorker(ctx context.Context) {
	mode := normalizeSimulationAlarmMode(s.cfg.SimulationAlarmMode)
	if mode == service.SimulationModeOff || s.db == nil {
		return
	}
	ticker := time.NewTicker(simulationTickInterval)
	defer ticker.Stop()

	tick := func() {
		if s.alarmMaintenance(ctx) {
			return
		}
		now := time.Now()
		// Drain existing records without creating or scheduling new slots.
		s.closeExpiredSimulationSlotsContext(ctx, now)
	}
	tick()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			tick()
		}
	}
}

func (s *Server) closeExpiredSimulationSlots(now time.Time) {
	s.closeExpiredSimulationSlotsContext(context.Background(), now)
}
func (s *Server) closeExpiredSimulationSlotsContext(parent context.Context, now time.Time) {
	ctx, cancel := context.WithTimeout(parent, inboxTransactionBudget)
	defer cancel()
	events, err := service.CloseExpiredSimulationSlots(s.db.WithContext(ctx), now)
	if err != nil {
		s.warnSimulation("simulation_close_failed", err)
		return
	}
	for _, event := range events {
		s.broadcast(event)
	}
}

func (s *Server) warnSimulation(message string, err error) {
	if s.log != nil {
		s.log.Warn(message, zap.Error(err))
	}
}
