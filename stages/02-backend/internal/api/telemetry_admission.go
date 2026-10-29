package api

import (
	"context"
	"sync"
	"time"

	mqtt "github.com/eclipse/paho.mqtt.golang"
)

// A durable receipt can complete concurrently, but MQTT QoS1 acknowledgements
// must follow delivery order. In particular EMQX durable sessions reject an ACK
// ahead of their uncommitted head. This buffer never owns durable queue truth.
type orderedAdmission struct {
	mqtt.Message
	durable chan struct{}
	once    sync.Once
	queued  time.Time
}

func (m *orderedAdmission) Ack() { m.once.Do(func() { close(m.durable) }) }

// Paho invokes this callback serially (OrderMatters=true). One shared admission
// budget includes queued receipts, active transactions and pending ACKs.
func (s *Server) enqueueAdmission(ctx context.Context, message mqtt.Message) {
	select {
	case <-ctx.Done():
		return
	case s.ingressSlots <- struct{}{}:
	}
	m := &orderedAdmission{Message: message, durable: make(chan struct{}), queued: time.Now()}
	select {
	case <-ctx.Done():
		<-s.ingressSlots
		return
	case s.ingressAcks <- m:
	}
	if len(message.Payload()) > 64*1024 {
		// No durable rejection record exists for an unparseable envelope. Keep
		// broker ownership and visible backpressure; never discard it via PUBACK.
		s.telemetry.touch("ingress_oversize", time.Now())
		s.telemetryLog("ingress_oversize_unacknowledged", time.Now())
		return
	}
	select {
	case <-ctx.Done():
		// Leave the real message unacknowledged for broker redelivery.
		return
	case s.ingressQueue <- m:
	}
}

func (s *Server) ackAdmissions(ctx context.Context) {
	for {
		var m *orderedAdmission
		select {
		case <-ctx.Done():
			return
		case m = <-s.ingressAcks:
		}
		s.ingressHead.Store(m.queued.UnixNano())
		timer := time.NewTimer(5 * time.Second)
		select {
		case <-ctx.Done():
			timer.Stop()
			return
		case <-m.durable:
			timer.Stop()
		case <-timer.C:
			s.telemetryLog("ingress_ack_head_wait", time.Now())
			select {
			case <-ctx.Done():
				return
			case <-m.durable:
			}
		}
		if ctx.Err() != nil {
			return
		}
		m.Message.Ack()
		s.telemetry.touch("ingress_ack_requested", time.Now())
		s.ingressHead.Store(0)
		<-s.ingressSlots
	}
}
