package ingest

import (
	"context"
	mqtt "github.com/eclipse/paho.mqtt.golang"
	"powersystem/backend/internal/model"
	"powersystem/backend/internal/telemetry"
	"time"
)

// QoS1 owns replay until the SQLite FULL transaction commits. The volatile
// buffer and per-commit batch are bounded; no registration cache survives a batch.
func (g *Gateway) acceptTelemetry(_ mqtt.Client, m mqtt.Message) {
	// Rejections must use the same FIFO as valid messages. A callback-side ACK
	// can overtake earlier messages still waiting for their SQLite commit.
	select {
	case <-g.runContext.Done():
		return
	case g.ingress <- m:
	}
}
func (g *Gateway) admissionWorker(ctx context.Context) {
	for {
		var first mqtt.Message
		select {
		case <-ctx.Done():
			return
		case first = <-g.ingress:
		}
		batch := []mqtt.Message{first}
		timer := time.NewTimer(20 * time.Millisecond)
	collect:
		for len(batch) < 32 {
			select {
			case <-ctx.Done():
				timer.Stop()
				return
			case m := <-g.ingress:
				batch = append(batch, m)
			case <-timer.C:
				break collect
			}
		}
		timer.Stop()
		for !g.persistAdmission(ctx, batch) {
			timer := time.NewTimer(time.Second)
			select {
			case <-ctx.Done():
				timer.Stop()
				return
			case <-timer.C:
			}
		}
	}
}
func (g *Gateway) persistAdmission(parent context.Context, batch []mqtt.Message) bool {
	ctx, cancel := context.WithTimeout(parent, 3*time.Second)
	defer cancel()
	samples := make([]telemetry.Sample, 0, len(batch))
	messages := make([]mqtt.Message, 0, len(batch))
	codes := make([]string, 0, len(batch))
	for _, m := range batch {
		p, e := telemetry.Parse(m.Payload())
		if e != nil {
			continue
		}
		samples = append(samples, p)
		messages = append(messages, m)
		codes = append(codes, p.DeviceCode)
	}
	if len(samples) == 0 {
		for _, m := range batch {
			m.Ack()
		}
		return true
	}
	var registered []model.Device
	if e := g.pg.WithContext(ctx).Where("device_code IN ? AND deleted_at IS NULL", codes).Find(&registered).Error; e != nil {
		g.log.Warn("gateway_admission_lookup_retry")
		return false
	}
	devices := make(map[string]model.Device, len(registered))
	for _, d := range registered {
		devices[d.DeviceCode] = d
	}
	tx, e := g.queue.BeginTx(ctx, nil)
	if e != nil {
		return false
	}
	defer tx.Rollback()
	for i, p := range samples {
		d, ok := devices[p.DeviceCode]
		if !ok || d.StationCode != p.StationCode {
			continue
		}
		if _, e := tx.ExecContext(ctx, "INSERT OR IGNORE INTO pending(run_id,device_id,seq,ts_ms,payload) VALUES(?,?,?,?,?)", p.RunID, d.ID, p.Seq, p.TS, string(messages[i].Payload())); e != nil {
			g.log.Warn("gateway_admission_persist_retry")
			return false
		}
	}
	if e := tx.Commit(); e != nil {
		g.log.Warn("gateway_admission_commit_retry")
		return false
	}
	// The single admission worker owns all ACKs, including rejected envelopes.
	// No ACK from this batch is sent if lookup or durable persistence failed.
	for _, m := range batch {
		m.Ack()
	}
	select {
	case g.wake <- struct{}{}:
	default:
	}
	return true
}
