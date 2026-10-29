package api

import (
	"context"
	"testing"
	"time"

	mqtt "github.com/eclipse/paho.mqtt.golang"
)

type ackOrderMessage struct {
	mqtt.Message
	id      int
	payload []byte
	acks    chan int
}

func (m ackOrderMessage) Payload() []byte { return m.payload }
func (m ackOrderMessage) Ack()            { m.acks <- m.id }

func admissionFixture(t *testing.T) (*Server, context.Context, context.CancelFunc, chan int) {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	s := &Server{ingressQueue: make(chan mqtt.Message, 128), ingressAcks: make(chan *orderedAdmission, 128), ingressSlots: make(chan struct{}, 128)}
	return s, ctx, cancel, make(chan int, 256)
}

func TestAdmissionAcknowledgementsFollowDelivery(t *testing.T) {
	s, ctx, cancel, acks := admissionFixture(t)
	defer cancel()
	go s.ackAdmissions(ctx)
	for i := 1; i <= 4; i++ {
		s.enqueueAdmission(ctx, ackOrderMessage{id: i, acks: acks})
	}
	var pending []mqtt.Message
	for i := 0; i < 4; i++ {
		pending = append(pending, <-s.ingressQueue)
	}
	for i := 3; i >= 1; i-- {
		pending[i].Ack()
		pending[i].Ack()
	}
	select {
	case <-acks:
		t.Fatal("later durable receipts acknowledged ahead of head")
	case <-time.After(20 * time.Millisecond):
	}
	pending[0].Ack()
	for i := 1; i <= 4; i++ {
		select {
		case actual := <-acks:
			if actual != i {
				t.Fatalf("ack %d, expected %d", actual, i)
			}
		case <-time.After(time.Second):
			t.Fatal("ack did not progress")
		}
	}
}

func TestAdmissionCancellationRetainsUndurableHead(t *testing.T) {
	s, ctx, cancel, acks := admissionFixture(t)
	go s.ackAdmissions(ctx)
	s.enqueueAdmission(ctx, ackOrderMessage{id: 1, acks: acks})
	s.enqueueAdmission(ctx, ackOrderMessage{id: 2, acks: acks})
	<-s.ingressQueue
	(<-s.ingressQueue).Ack()
	cancel()
	select {
	case <-acks:
		t.Fatal("cancel acknowledged undurable head or successor")
	case <-time.After(20 * time.Millisecond):
	}
}

func TestAdmissionCancelledAfterCommitDoesNotRequestAck(t *testing.T) {
	s, ctx, cancel, acks := admissionFixture(t)
	s.enqueueAdmission(ctx, ackOrderMessage{id: 1, acks: acks})
	(<-s.ingressQueue).Ack()
	cancel()
	s.ackAdmissions(ctx)
	select {
	case <-acks:
		t.Fatal("cancelled committed receipt acknowledged")
	default:
	}
}

func TestAdmissionBudgetIncludesPendingAcknowledgements(t *testing.T) {
	s, ctx, cancel, acks := admissionFixture(t)
	defer cancel()
	for i := 0; i < 128; i++ {
		s.enqueueAdmission(ctx, ackOrderMessage{id: i, acks: acks})
		(<-s.ingressQueue).Ack()
	}
	done := make(chan struct{})
	go func() { s.enqueueAdmission(ctx, ackOrderMessage{id: 128, acks: acks}); close(done) }()
	select {
	case <-done:
		t.Fatal("admitted beyond shared 128 budget")
	case <-time.After(20 * time.Millisecond):
	}
	go s.ackAdmissions(ctx)
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("budget did not release after ACK")
	}
}

func TestOversizeRemainsUnacknowledgedAndVisible(t *testing.T) {
	s, ctx, cancel, acks := admissionFixture(t)
	defer cancel()
	go s.ackAdmissions(ctx)
	s.enqueueAdmission(ctx, ackOrderMessage{id: 1, acks: acks})
	s.enqueueAdmission(ctx, ackOrderMessage{id: 2, acks: acks, payload: make([]byte, 65537)})
	select {
	case <-acks:
		t.Fatal("oversize ACK overtook head")
	case <-time.After(20 * time.Millisecond):
	}
	(<-s.ingressQueue).Ack()
	select {
	case got := <-acks:
		if got != 1 {
			t.Fatal(got)
		}
	case <-time.After(time.Second):
		t.Fatal("missing head ACK")
	}
	select {
	case <-acks:
		t.Fatal("oversize was silently acknowledged")
	case <-time.After(20 * time.Millisecond):
	}
	if s.telemetry.counts["ingress_oversize"] != 1 {
		t.Fatal("oversize not observable")
	}
}
