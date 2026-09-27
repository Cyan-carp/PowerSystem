package ingest

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	mqtt "github.com/eclipse/paho.mqtt.golang"
	"go.uber.org/zap"
	"gorm.io/gorm"
	_ "modernc.org/sqlite"

	"powersystem/backend/internal/config"
	"powersystem/backend/internal/model"
	"powersystem/backend/internal/tdengine"
	"powersystem/backend/internal/telemetry"
)

type Gateway struct {
	cfg   config.Config
	pg    *gorm.DB
	queue *sql.DB
	td    *tdengine.Client
	log   *zap.Logger
	wake  chan struct{}
}
type queued struct {
	runID    string
	deviceID int64
	seq      int64
	sample   telemetry.Sample
}

func NewGateway(cfg config.Config, pg *gorm.DB, td *tdengine.Client, log *zap.Logger) (*Gateway, error) {
	if err := os.MkdirAll(filepath.Dir(cfg.QueuePath), 0755); err != nil {
		return nil, err
	}
	queue, err := sql.Open("sqlite", cfg.QueuePath)
	if err != nil {
		return nil, err
	}
	queue.SetMaxOpenConns(1)
	for _, statement := range []string{
		"PRAGMA journal_mode=WAL", "PRAGMA synchronous=FULL", "PRAGMA busy_timeout=5000",
		"CREATE TABLE IF NOT EXISTS pending(run_id TEXT NOT NULL,device_id INTEGER NOT NULL,seq INTEGER NOT NULL,ts_ms INTEGER NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(run_id,device_id,seq))",
	} {
		if _, err = queue.Exec(statement); err != nil {
			queue.Close()
			return nil, err
		}
	}
	return &Gateway{cfg: cfg, pg: pg, queue: queue, td: td, log: log, wake: make(chan struct{}, 1)}, nil
}
func (g *Gateway) Close() error { return g.queue.Close() }

func (g *Gateway) Run(ctx context.Context) error {
	ctx, stop := signal.NotifyContext(ctx, os.Interrupt, syscall.SIGTERM)
	defer stop()
	go g.writer(ctx)
	path := g.cfg.QueuePath + ".mqtt-store"
	if err := os.MkdirAll(path, 0755); err != nil {
		return err
	}
	opts := mqtt.NewClientOptions().AddBroker(g.cfg.Broker).SetClientID(g.cfg.ClientID).SetCleanSession(false).SetStore(mqtt.NewFileStore(path)).SetAutoReconnect(true).SetConnectRetry(true).SetConnectRetryInterval(time.Second).SetMaxReconnectInterval(30 * time.Second).SetAutoAckDisabled(true).SetOrderMatters(true)
	opts.SetOnConnectHandler(func(c mqtt.Client) {
		token := c.Subscribe("device/telemetry", 1, g.receive)
		if !token.WaitTimeout(10*time.Second) || token.Error() != nil {
			g.log.Error("subscribe_failed", zap.Error(token.Error()))
		} else {
			g.log.Info("gateway_subscribed")
		}
	})
	client := mqtt.NewClient(opts)
	client.AddRoute("device/telemetry", g.receive)
	token := client.Connect()
	if !token.WaitTimeout(30*time.Second) || token.Error() != nil {
		return fmt.Errorf("mqtt connect: %v", token.Error())
	}
	g.log.Info("gateway_started", zap.String("database", g.cfg.TDDatabase))
	<-ctx.Done()
	client.Disconnect(500)
	return nil
}
func (g *Gateway) receive(_ mqtt.Client, message mqtt.Message) {
	sample, err := telemetry.Parse(message.Payload())
	if err != nil {
		g.log.Warn("telemetry_rejected", zap.Error(err))
		message.Ack()
		return
	}
	var device model.Device
	if err := g.pg.Where("device_code = ? AND deleted_at IS NULL", sample.DeviceCode).First(&device).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			g.log.Warn("unregistered_device", zap.String("device", sample.DeviceCode))
			message.Ack()
		} else {
			g.log.Error("device_lookup_failed", zap.Error(err))
		}
		return
	}
	if device.StationCode != sample.StationCode {
		g.log.Warn("station_mismatch", zap.String("device", sample.DeviceCode))
		message.Ack()
		return
	}
	_, err = g.queue.Exec("INSERT OR IGNORE INTO pending(run_id,device_id,seq,ts_ms,payload) VALUES(?,?,?,?,?)", sample.RunID, device.ID, sample.Seq, sample.TS, string(message.Payload()))
	if err != nil {
		g.log.Error("queue_write_failed", zap.Error(err))
		return
	}
	message.Ack()
	select {
	case g.wake <- struct{}{}:
	default:
	}
}
func (g *Gateway) writer(ctx context.Context) {
	ticker := time.NewTicker(2 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			g.flush(ctx)
		case <-g.wake:
			var n int
			if err := g.queue.QueryRow("SELECT COUNT(*) FROM pending").Scan(&n); err == nil && n >= 50 {
				g.flush(ctx)
			}
		}
	}
}
func (g *Gateway) flush(ctx context.Context) {
	for {
		rows, err := g.queue.QueryContext(ctx, "SELECT run_id,device_id,seq,payload FROM pending ORDER BY ts_ms,device_id LIMIT 50")
		if err != nil {
			g.log.Error("queue_read_failed", zap.Error(err))
			return
		}
		batch := make([]queued, 0, 50)
		for rows.Next() {
			var row queued
			var payload string
			if err = rows.Scan(&row.runID, &row.deviceID, &row.seq, &payload); err != nil {
				break
			}
			if err = json.Unmarshal([]byte(payload), &row.sample); err != nil {
				break
			}
			batch = append(batch, row)
		}
		rows.Close()
		if err != nil {
			g.log.Error("queue_decode_failed", zap.Error(err))
			return
		}
		if len(batch) == 0 {
			return
		}
		groups := make(map[int64][]telemetry.Sample)
		for _, row := range batch {
			groups[row.deviceID] = append(groups[row.deviceID], row.sample)
		}
		for id, samples := range groups {
			var device model.Device
			if err = g.pg.First(&device, id).Error; err != nil {
				g.log.Error("device_lookup_failed", zap.Error(err))
				return
			}
			if err = g.td.Insert(ctx, device, samples); err != nil {
				g.log.Warn("tdengine_write_failed", zap.Error(err), zap.Int64("device_id", id))
				return
			}
		}
		tx, err := g.queue.BeginTx(ctx, nil)
		if err != nil {
			g.log.Error("queue_transaction_failed", zap.Error(err))
			return
		}
		for _, row := range batch {
			if _, err = tx.ExecContext(ctx, "DELETE FROM pending WHERE run_id=? AND device_id=? AND seq=?", row.runID, row.deviceID, row.seq); err != nil {
				break
			}
		}
		if err != nil {
			_ = tx.Rollback()
			g.log.Error("queue_delete_failed", zap.Error(err))
			return
		}
		if err = tx.Commit(); err != nil {
			g.log.Error("queue_commit_failed", zap.Error(err))
			return
		}
		g.log.Info("tdengine_batch_written", zap.Int("count", len(batch)))
		if len(batch) < 50 {
			return
		}
	}
}
