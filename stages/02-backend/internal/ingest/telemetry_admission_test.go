package ingest

import (
	"context"
	"encoding/json"
	"math"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	mqtt "github.com/eclipse/paho.mqtt.golang"
	"go.uber.org/zap"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"powersystem/backend/internal/config"
	"powersystem/backend/internal/telemetry"
)

type admissionTestMessage struct {
	id  uint16
	b   []byte
	ack func(uint16)
}

func (m admissionTestMessage) Duplicate() bool   { return false }
func (m admissionTestMessage) Qos() byte         { return 1 }
func (m admissionTestMessage) Retained() bool    { return false }
func (m admissionTestMessage) Topic() string     { return "device/telemetry" }
func (m admissionTestMessage) MessageID() uint16 { return m.id }
func (m admissionTestMessage) Payload() []byte   { return m.b }
func (m admissionTestMessage) Ack()              { m.ack(m.id) }

func admissionFixture(t *testing.T) *Gateway {
	t.Helper()
	dsn := os.Getenv("TR_ACK_TEST_DSN")
	if dsn == "" {
		t.Skip("requires dedicated TR_ACK_TEST_DSN")
	}
	db, err := gorm.Open(postgres.Open(dsn), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		t.Fatal("test database unavailable")
	}
	var database string
	if err = db.Raw("SELECT current_database()").Scan(&database).Error; err != nil || !strings.HasPrefix(database, "tr_ack_20261030_") {
		t.Fatal("requires isolated tr_ack_20261030_* database")
	}
	if err = db.Exec("CREATE TABLE IF NOT EXISTS devices(id bigint PRIMARY KEY,device_code text,station_code text,deleted_at timestamptz); INSERT INTO devices VALUES(1,'ACK-01','ACK-ST',NULL) ON CONFLICT DO NOTHING").Error; err != nil {
		t.Fatal(err)
	}
	g, err := NewGateway(config.Config{QueuePath: filepath.Join(t.TempDir(), "gateway.sqlite")}, db, nil, zap.NewNop())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { g.Close(); sqlDB, _ := db.DB(); sqlDB.Close() })
	return g
}

func admissionPayload(t *testing.T, seq int64, temperature float64) []byte {
	t.Helper()
	p := telemetry.Sample{SchemaVersion: 1, RunID: "ack-regression", DeviceCode: "ACK-01", StationCode: "ACK-ST", Seq: seq, TS: time.Now().UnixMilli(), Voltage: 400, Current: 10, Power: math.Sqrt(3) * 400 * 10 * .99 / 1000, Temperature: temperature, Status: 1}
	b, err := json.Marshal(p)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func TestGatewayMixedBatchACKOrderAndDurability(t *testing.T) {
	g := admissionFixture(t)
	var acks []uint16
	ack := func(id uint16) {
		var persisted int
		if err := g.queue.QueryRow("SELECT count(*) FROM pending").Scan(&persisted); err != nil || persisted != 3 {
			t.Fatalf("ACK %d before durable batch commit: rows=%d", id, persisted)
		}
		acks = append(acks, id)
	}
	batch := []mqtt.Message{
		admissionTestMessage{1, admissionPayload(t, 1, 48), ack},
		admissionTestMessage{2, admissionPayload(t, 2, 92), ack},
		admissionTestMessage{3, admissionPayload(t, 3, 49), ack},
		admissionTestMessage{4, []byte("{invalid"), ack},
		admissionTestMessage{5, []byte(strings.Repeat("x", 64*1024+1)), ack},
		admissionTestMessage{6, admissionPayload(t, 6, 50), ack},
	}
	if !g.persistAdmission(context.Background(), batch) {
		t.Fatal("admission failed")
	}
	if !reflect.DeepEqual(acks, []uint16{1, 2, 3, 4, 5, 6}) {
		t.Fatalf("ACK order %v", acks)
	}
}

func TestGatewayPersistenceFailureDoesNotACKRejectedTail(t *testing.T) {
	g := admissionFixture(t)
	var acks []uint16
	ack := func(id uint16) { acks = append(acks, id) }
	batch := []mqtt.Message{admissionTestMessage{1, admissionPayload(t, 1, 48), ack}, admissionTestMessage{2, admissionPayload(t, 2, 92), ack}}
	if _, err := g.queue.Exec("CREATE TRIGGER fail_admission BEFORE INSERT ON pending BEGIN SELECT RAISE(ABORT,'injected persistence failure'); END"); err != nil {
		t.Fatal(err)
	}
	if g.persistAdmission(context.Background(), batch) || len(acks) != 0 {
		t.Fatalf("failed commit acknowledged %v", acks)
	}
	if _, err := g.queue.Exec("DROP TRIGGER fail_admission"); err != nil {
		t.Fatal(err)
	}
	if !g.persistAdmission(context.Background(), batch) || !reflect.DeepEqual(acks, []uint16{1, 2}) {
		t.Fatalf("retry ACK order %v", acks)
	}
}

func TestGatewayOversizeUsesAdmissionFIFO(t *testing.T) {
	g := &Gateway{runContext: context.Background(), ingress: make(chan mqtt.Message, 1)}
	acked := false
	m := admissionTestMessage{1, []byte(strings.Repeat("x", 64*1024+1)), func(uint16) { acked = true }}
	g.acceptTelemetry(nil, m)
	if acked || len(g.ingress) != 1 {
		t.Fatal("oversize bypassed admission FIFO")
	}
	if !g.persistAdmission(context.Background(), []mqtt.Message{<-g.ingress}) || !acked {
		t.Fatal("rejected-only batch not acknowledged")
	}
}
