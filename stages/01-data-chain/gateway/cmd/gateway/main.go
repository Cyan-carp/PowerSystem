package main

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"math"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"regexp"
	"strings"
	"syscall"
	"time"

	mqtt "github.com/eclipse/paho.mqtt.golang"
	_ "modernc.org/sqlite"
)

type Telemetry struct {
	SchemaVersion int     `json:"schema_version"`
	RunID         string  `json:"run_id"`
	DeviceID      string  `json:"device_id"`
	StationID     string  `json:"station_id"`
	Seq           int64   `json:"seq"`
	TS            int64   `json:"ts_ms"`
	Voltage       float64 `json:"voltage"`
	Current       float64 `json:"current"`
	Temperature   float64 `json:"temperature"`
	Power         float64 `json:"power"`
	Status        int     `json:"status"`
	FaultCode     int     `json:"fault_code"`
}

type Config struct {
	Broker       string
	QueuePath    string
	ShutdownFile string
	TDURL        string
	TDPassword   string
	TDDatabase   string
	ClientID     string
}

type Gateway struct {
	cfg    Config
	db     *sql.DB
	http   *http.Client
	logger *slog.Logger
	wake   chan struct{}
}

var databaseName = regexp.MustCompile(`^[a-z][a-z0-9_]{0,63}$`)

func main() {
	var cfg Config
	flag.StringVar(&cfg.Broker, "broker", "tcp://127.0.0.1:1883", "MQTT broker URL")
	flag.StringVar(&cfg.QueuePath, "queue", "data/gateway.sqlite", "durable SQLite queue")
	flag.StringVar(&cfg.ShutdownFile, "shutdown-file", "", "file that requests a graceful restart")
	flag.StringVar(&cfg.TDURL, "td-url", "http://127.0.0.1:6041", "TDengine REST base URL")
	flag.StringVar(&cfg.TDDatabase, "td-database", "powersystem", "TDengine database")
	flag.StringVar(&cfg.ClientID, "client-id", "powersystem-stage1-gateway", "stable MQTT client ID")
	flag.Parse()
	cfg.TDPassword = os.Getenv("TDENGINE_ROOT_PASSWORD")
	if cfg.TDPassword == "" || !databaseName.MatchString(cfg.TDDatabase) {
		fmt.Fprintln(os.Stderr, "TDENGINE_ROOT_PASSWORD is required and td-database must be a safe SQL identifier")
		os.Exit(2)
	}
	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	if err := os.MkdirAll(filepath.Dir(cfg.QueuePath), 0o755); err != nil {
		logger.Error("queue_directory_failed", "error", err)
		os.Exit(1)
	}
	db, err := sql.Open("sqlite", cfg.QueuePath)
	if err != nil {
		logger.Error("queue_open_failed", "error", err)
		os.Exit(1)
	}
	defer db.Close()
	db.SetMaxOpenConns(1)
	for _, stmt := range []string{
		"PRAGMA journal_mode=WAL",
		"PRAGMA synchronous=FULL",
		"PRAGMA busy_timeout=5000",
		"CREATE TABLE IF NOT EXISTS pending (device_id TEXT NOT NULL, seq INTEGER NOT NULL, ts_ms INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(device_id,seq))",
	} {
		if _, err := db.Exec(stmt); err != nil {
			logger.Error("queue_init_failed", "error", err)
			os.Exit(1)
		}
	}
	g := &Gateway{cfg: cfg, db: db, http: &http.Client{Timeout: 15 * time.Second}, logger: logger, wake: make(chan struct{}, 1)}
	mqttStorePath := cfg.QueuePath + ".mqtt-store"
	if err := os.MkdirAll(mqttStorePath, 0o755); err != nil {
		logger.Error("mqtt_store_directory_failed", "error", err)
		os.Exit(1)
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	go g.writer(ctx)
	if cfg.ShutdownFile != "" {
		go func() {
			ticker := time.NewTicker(250 * time.Millisecond)
			defer ticker.Stop()
			for {
				select {
				case <-ctx.Done():
					return
				case <-ticker.C:
					if _, err := os.Stat(cfg.ShutdownFile); err == nil {
						logger.Info("graceful_restart_requested")
						stop()
						return
					}
				}
			}
		}()
	}

	opts := mqtt.NewClientOptions().AddBroker(cfg.Broker).
		SetClientID(cfg.ClientID).
		SetCleanSession(false).
		SetStore(mqtt.NewFileStore(mqttStorePath)).
		SetAutoReconnect(true).
		SetMaxReconnectInterval(30 * time.Second).
		SetConnectRetry(true).
		SetConnectRetryInterval(time.Second).
		SetAutoAckDisabled(true).
		SetOrderMatters(true)
	opts.SetConnectionLostHandler(func(_ mqtt.Client, err error) {
		logger.Warn("mqtt_connection_lost", "error", err)
	})
	opts.SetOnConnectHandler(func(c mqtt.Client) {
		logger.Info("mqtt_connected", "client_id", cfg.ClientID)
		token := c.Subscribe("device/telemetry", 1, g.receive)
		if !token.WaitTimeout(10*time.Second) || token.Error() != nil {
			logger.Error("mqtt_subscribe_failed", "error", token.Error())
		} else {
			logger.Info("mqtt_subscribed", "topic", "device/telemetry")
		}
	})
	client := mqtt.NewClient(opts)
	client.AddRoute("device/telemetry", g.receive)
	connect := client.Connect()
	if !connect.WaitTimeout(30*time.Second) || connect.Error() != nil {
		logger.Error("mqtt_initial_connect_failed", "error", connect.Error())
		os.Exit(1)
	}
	logger.Info("gateway_started", "queue", cfg.QueuePath, "database", cfg.TDDatabase)
	<-ctx.Done()
	client.Disconnect(500)
	logger.Info("gateway_stopped")
}

func validate(t Telemetry) error {
	if t.SchemaVersion != 1 || t.RunID == "" || len(t.RunID) > 64 || t.StationID != "ST-01" || t.Seq < 0 {
		return errors.New("invalid schema, run, station, or sequence")
	}
	if t.DeviceID != "INV-1001" && t.DeviceID != "INV-1002" && t.DeviceID != "INV-1003" {
		return errors.New("unknown device")
	}
	if t.TS < time.Now().Add(-48*time.Hour).UnixMilli() || t.TS > time.Now().Add(48*time.Hour).UnixMilli() {
		return errors.New("timestamp outside accepted window")
	}
	for _, v := range []float64{t.Voltage, t.Current, t.Temperature, t.Power} {
		if math.IsNaN(v) || math.IsInf(v, 0) {
			return errors.New("non-finite number")
		}
	}
	if t.Voltage < 320 || t.Voltage > 460 || t.Current < 0 || t.Current > 160.4 || t.Power < 0 || t.Power > 110 || t.Temperature < -30 || t.Temperature > 85 {
		return errors.New("value outside physical envelope")
	}
	if (t.Status != 0 && t.Status != 1 && t.Status != 2) || t.FaultCode < 0 || t.FaultCode > 9999 {
		return errors.New("invalid state or fault code")
	}
	if math.Abs(t.Power-(math.Sqrt(3)*t.Voltage*t.Current*0.99/1000)) > 2 {
		return errors.New("power inconsistent with voltage/current")
	}
	return nil
}

func (g *Gateway) receive(_ mqtt.Client, message mqtt.Message) {
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(message.Payload(), &fields); err != nil {
		g.logger.Warn("telemetry_rejected", "reason", "invalid_json", "error", err)
		message.Ack()
		return
	}
	for _, key := range []string{"schema_version", "run_id", "device_id", "station_id", "seq", "ts_ms", "voltage", "current", "temperature", "power", "status", "fault_code"} {
		if _, ok := fields[key]; !ok {
			g.logger.Warn("telemetry_rejected", "reason", "missing_field", "field", key)
			message.Ack()
			return
		}
	}
	var t Telemetry
	decoder := json.NewDecoder(bytes.NewReader(message.Payload()))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&t); err != nil {
		g.logger.Warn("telemetry_rejected", "reason", "invalid_json", "error", err)
		message.Ack()
		return
	}
	var extra any
	if err := decoder.Decode(&extra); err != io.EOF {
		g.logger.Warn("telemetry_rejected", "reason", "trailing_json")
		message.Ack()
		return
	}
	if err := validate(t); err != nil {
		g.logger.Warn("telemetry_rejected", "reason", err, "device_id", t.DeviceID, "seq", t.Seq)
		message.Ack()
		return
	}
	g.logger.Info("telemetry_received", "device_id", t.DeviceID, "seq", t.Seq, "mqtt_message_id", message.MessageID())
	_, err := g.db.Exec("INSERT OR IGNORE INTO pending(device_id,seq,ts_ms,payload) VALUES(?,?,?,?)", t.DeviceID, t.Seq, t.TS, string(message.Payload()))
	if err != nil {
		g.logger.Error("queue_write_failed", "error", err, "device_id", t.DeviceID, "seq", t.Seq)
		return
	}
	message.Ack()
	g.logger.Info("telemetry_queued", "device_id", t.DeviceID, "seq", t.Seq)
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
		case <-g.wake:
			g.flush(ctx, false)
		case <-ticker.C:
			g.flush(ctx, true)
		}
	}
}

type pendingRow struct {
	device string
	seq    int64
	data   Telemetry
}

func (g *Gateway) flush(ctx context.Context, force bool) {
	for {
		rows, err := g.db.QueryContext(ctx, "SELECT device_id,seq,payload FROM pending ORDER BY ts_ms,device_id LIMIT 50")
		if err != nil {
			if ctx.Err() == nil {
				g.logger.Error("queue_read_failed", "error", err)
			}
			return
		}
		batch := make([]pendingRow, 0, 50)
		for rows.Next() {
			var row pendingRow
			var payload string
			if err := rows.Scan(&row.device, &row.seq, &payload); err != nil {
				g.logger.Error("queue_scan_failed", "error", err)
				break
			}
			if err := json.Unmarshal([]byte(payload), &row.data); err != nil {
				g.logger.Error("queue_payload_corrupt", "error", err, "device_id", row.device, "seq", row.seq)
				break
			}
			batch = append(batch, row)
		}
		rows.Close()
		if len(batch) == 0 || (!force && len(batch) < 50) {
			return
		}
		query := buildInsert(g.cfg.TDDatabase, batch)
		if err := g.postSQL(ctx, query); err != nil {
			g.logger.Warn("tdengine_write_failed", "error", err, "batch_size", len(batch))
			return
		}
		tx, err := g.db.BeginTx(ctx, nil)
		if err != nil {
			g.logger.Error("queue_delete_failed", "error", err)
			return
		}
		for _, row := range batch {
			if _, err = tx.ExecContext(ctx, "DELETE FROM pending WHERE device_id=? AND seq=?", row.device, row.seq); err != nil {
				break
			}
		}
		if err == nil {
			err = tx.Commit()
		} else {
			_ = tx.Rollback()
		}
		if err != nil {
			g.logger.Error("queue_delete_failed", "error", err)
			return
		}
		g.logger.Info("tdengine_batch_written", "count", len(batch))
		force = false
	}
}

func buildInsert(database string, batch []pendingRow) string {
	grouped := map[string][]Telemetry{}
	for _, row := range batch {
		grouped[row.device] = append(grouped[row.device], row.data)
	}
	var sqlText strings.Builder
	sqlText.WriteString("INSERT INTO")
	for _, device := range []string{"INV-1001", "INV-1002", "INV-1003"} {
		items := grouped[device]
		if len(items) == 0 {
			continue
		}
		sqlText.WriteString(" ")
		sqlText.WriteString(database)
		sqlText.WriteString(".t_inv_")
		sqlText.WriteString(strings.TrimPrefix(device, "INV-"))
		sqlText.WriteString(" VALUES")
		for _, t := range items {
			fmt.Fprintf(&sqlText, " (%d,%d,%.3f,%.3f,%.3f,%.3f,%d,%d)", t.TS, t.Seq, t.Voltage, t.Current, t.Temperature, t.Power, t.Status, t.FaultCode)
		}
	}
	return sqlText.String()
}

func (g *Gateway) postSQL(ctx context.Context, query string) error {
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, strings.TrimRight(g.cfg.TDURL, "/")+"/rest/sql", strings.NewReader(query))
	if err != nil {
		return err
	}
	request.SetBasicAuth("root", g.cfg.TDPassword)
	request.Header.Set("Content-Type", "text/plain")
	response, err := g.http.Do(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	body, err := io.ReadAll(io.LimitReader(response.Body, 1<<20))
	if err != nil {
		return err
	}
	if response.StatusCode != http.StatusOK {
		return fmt.Errorf("HTTP %d: %s", response.StatusCode, string(body))
	}
	var result struct {
		Code int    `json:"code"`
		Desc string `json:"desc"`
	}
	if err := json.Unmarshal(body, &result); err != nil {
		return err
	}
	if result.Code != 0 {
		return fmt.Errorf("TDengine %d: %s", result.Code, result.Desc)
	}
	return nil
}
