package api

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	mqtt "github.com/eclipse/paho.mqtt.golang"
	"github.com/gin-gonic/gin"
	"github.com/golang-jwt/jwt/v5"
	"github.com/gorilla/websocket"
	"github.com/redis/go-redis/v9"
	"go.uber.org/zap"
	"gorm.io/gorm"

	"powersystem/backend/internal/config"
	"powersystem/backend/internal/model"
	"powersystem/backend/internal/service"
	"powersystem/backend/internal/tdengine"
	"powersystem/backend/internal/telemetry"
)

type Server struct {
	cfg       config.Config
	db        *gorm.DB
	redis     *redis.Client
	td        *tdengine.Client
	log       *zap.Logger
	router    *gin.Engine
	clients   map[*websocket.Conn]struct{}
	clientsMu sync.Mutex
}

func New(cfg config.Config, db *gorm.DB, redis *redis.Client, td *tdengine.Client, log *zap.Logger) *Server {
	gin.SetMode(gin.ReleaseMode)
	s := &Server{cfg: cfg, db: db, redis: redis, td: td, log: log, clients: make(map[*websocket.Conn]struct{})}
	r := gin.New()
	r.Use(gin.Recovery())
	r.GET("/api/v1/ping", func(c *gin.Context) { ok(c, gin.H{"status": "ok"}) })
	r.POST("/api/v1/auth/register", s.register)
	r.POST("/api/v1/auth/login", s.login)
	a := r.Group("/api/v1", s.authorize)
	a.GET("/devices", s.listDevices)
	a.POST("/devices", s.createDevice)
	a.GET("/devices/:id", s.getDevice)
	a.PUT("/devices/:id", s.updateDevice)
	a.DELETE("/devices/:id", s.deleteDevice)
	a.GET("/devices/:id/telemetry", s.history)
	a.GET("/devices/:id/telemetry/latest", s.latest)
	a.GET("/devices/:id/prediction", s.getPrediction)
	a.GET("/alarm-rules", s.listRules)
	a.POST("/alarm-rules", s.createRule)
	a.GET("/alarm-rules/:id", s.getRule)
	a.PUT("/alarm-rules/:id", s.updateRule)
	a.DELETE("/alarm-rules/:id", s.deleteRule)
	a.GET("/alarms", s.listAlarms)
	a.GET("/alarms/:id", s.getAlarm)
	a.POST("/alarms/:id/ack", s.ackAlarm)
	a.GET("/dashboard/summary", s.dashboard)
	a.POST("/ws-ticket", s.ticket)
	r.GET("/ws/realtime", s.websocket)
	r.GET("/test/ws", func(c *gin.Context) { c.File("test/ws.html") })
	s.router = r
	return s
}
func (s *Server) Run(ctx context.Context) error {
	ctx, stop := signal.NotifyContext(ctx, os.Interrupt, syscall.SIGTERM)
	defer stop()
	s.restoreLatest(ctx)
	go s.worker(ctx)
	if s.cfg.AIEnabled {
		go s.predictionWorker(ctx)
	}
	if err := s.subscribe(ctx); err != nil {
		return err
	}
	httpServer := &http.Server{Addr: s.cfg.HTTPAddr, Handler: s.router, ReadHeaderTimeout: 5 * time.Second}
	errCh := make(chan error, 1)
	go func() { errCh <- httpServer.ListenAndServe() }()
	s.log.Info("api_started", zap.String("address", s.cfg.HTTPAddr))
	select {
	case <-ctx.Done():
		shutCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		return httpServer.Shutdown(shutCtx)
	case err := <-errCh:
		if err == http.ErrServerClosed {
			return nil
		}
		return err
	}
}
func (s *Server) subscribe(ctx context.Context) error {
	storePath := s.cfg.QueuePath + ".mqtt-store"
	if err := os.MkdirAll(filepath.Dir(storePath), 0755); err != nil {
		return err
	}
	if err := os.MkdirAll(storePath, 0755); err != nil {
		return err
	}
	opts := mqtt.NewClientOptions().AddBroker(s.cfg.Broker).SetClientID(s.cfg.ClientID).SetCleanSession(false).SetStore(mqtt.NewFileStore(storePath)).SetAutoReconnect(true).SetConnectRetry(true).SetConnectRetryInterval(time.Second).SetMaxReconnectInterval(30 * time.Second).SetAutoAckDisabled(true).SetOrderMatters(true)
	opts.SetOnConnectHandler(func(c mqtt.Client) {
		token := c.Subscribe("device/telemetry", 1, s.receive)
		if !token.WaitTimeout(10*time.Second) || token.Error() != nil {
			s.log.Error("backend_subscribe_failed", zap.Error(token.Error()))
		}
	})
	client := mqtt.NewClient(opts)
	client.AddRoute("device/telemetry", s.receive)
	token := client.Connect()
	if !token.WaitTimeout(30*time.Second) || token.Error() != nil {
		return fmt.Errorf("mqtt connect: %v", token.Error())
	}
	go func() { <-ctx.Done(); client.Disconnect(500) }()
	return nil
}
func (s *Server) receive(_ mqtt.Client, message mqtt.Message) {
	sample, err := telemetry.Parse(message.Payload())
	if err != nil {
		s.log.Warn("backend_rejected", zap.Error(err))
		message.Ack()
		return
	}
	var device model.Device
	if err = s.db.Where("device_code = ? AND deleted_at IS NULL", sample.DeviceCode).First(&device).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			message.Ack()
		} else {
			s.log.Error("inbox_device_lookup", zap.Error(err))
		}
		return
	}
	if device.StationCode != sample.StationCode {
		message.Ack()
		return
	}
	err = s.db.Exec("INSERT INTO telemetry_inbox(run_id,device_id,seq,ts_ms,payload) VALUES(?,?,?,?,?::jsonb) ON CONFLICT(run_id,device_id,seq) DO NOTHING", sample.RunID, device.ID, sample.Seq, sample.TS, string(message.Payload())).Error
	if err != nil {
		s.log.Error("inbox_insert_failed", zap.Error(err))
		return
	}
	message.Ack()
}
func (s *Server) worker(ctx context.Context) {
	ticker := time.NewTicker(500 * time.Millisecond)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			s.processPending(ctx)
		}
	}
}
func (s *Server) processPending(ctx context.Context) {
	var items []model.Inbox
	// Each device advances in inbox order. A late TDengine row for one device
	// must not hold back healthy devices.
	if err := s.db.Raw("SELECT DISTINCT ON (device_id) * FROM telemetry_inbox WHERE processed_at IS NULL ORDER BY device_id,id LIMIT 500").Scan(&items).Error; err != nil {
		s.log.Error("inbox_read_failed", zap.Error(err))
		return
	}
	for _, item := range items {
		var sample telemetry.Sample
		if err := json.Unmarshal(item.Payload, &sample); err != nil {
			s.log.Error("inbox_payload_corrupt", zap.Error(err))
			return
		}
		found, err := s.td.HasSample(ctx, item.DeviceID, item.TS, item.Seq)
		if err != nil {
			s.log.Warn("tdengine_verify_failed", zap.Error(err))
			return
		}
		if !found {
			continue
		}
		var device model.Device
		if err := s.db.First(&device, item.DeviceID).Error; err != nil {
			s.log.Error("inbox_device_lookup_failed", zap.Error(err))
			return
		}
		latest, err := s.cacheLatest(ctx, device, sample, item.ReceivedAt)
		if err != nil {
			s.log.Warn("redis_latest_failed", zap.Error(err))
			return
		}
		_, events, err := service.ProcessSample(s.db, item, sample)
		if err != nil {
			s.log.Error("inbox_process_failed", zap.Error(err))
			return
		}
		s.broadcast(gin.H{"type": "telemetry", "data": latest})
		for _, event := range events {
			s.broadcast(event)
		}
	}
}

func (s *Server) cacheLatest(ctx context.Context, device model.Device, sample telemetry.Sample, receivedAt time.Time) (gin.H, error) {
	latest := gin.H{"device_id": device.ID, "device_code": device.DeviceCode, "run_id": sample.RunID, "seq": sample.Seq, "ts_ms": sample.TS, "received_at": receivedAt, "voltage": sample.Voltage, "current": sample.Current, "temperature": sample.Temperature, "power": sample.Power, "status": sample.Status, "fault_code": sample.FaultCode}
	b, err := json.Marshal(latest)
	if err != nil {
		return nil, err
	}
	if err := s.redis.Set(ctx, fmt.Sprintf("device:%d:latest", device.ID), b, 0).Err(); err != nil {
		return nil, err
	}
	return latest, nil
}

func (s *Server) restoreLatest(ctx context.Context) {
	var items []model.Inbox
	if err := s.db.Raw("SELECT DISTINCT ON (device_id) * FROM telemetry_inbox WHERE processed_at IS NOT NULL ORDER BY device_id,id DESC").Scan(&items).Error; err != nil {
		s.log.Warn("latest_restore_failed", zap.Error(err))
		return
	}
	for _, item := range items {
		var device model.Device
		if err := s.db.Where("id=? AND deleted_at IS NULL", item.DeviceID).First(&device).Error; err != nil {
			continue
		}
		var sample telemetry.Sample
		if err := json.Unmarshal(item.Payload, &sample); err != nil {
			continue
		}
		if _, err := s.cacheLatest(ctx, device, sample, item.ReceivedAt); err != nil {
			s.log.Warn("latest_restore_failed", zap.Error(err))
		}
	}
}

type response struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
	Data    any    `json:"data"`
}

func ok(c *gin.Context, data any) { c.JSON(http.StatusOK, response{0, "success", data}) }
func fail(c *gin.Context, status, code int, message string) {
	c.AbortWithStatusJSON(status, response{code, message, nil})
}
func page(c *gin.Context) (int, int, bool) {
	p, e1 := strconv.Atoi(c.DefaultQuery("page", "1"))
	size, e2 := strconv.Atoi(c.DefaultQuery("page_size", "20"))
	if e1 != nil || e2 != nil || p < 1 || size < 1 || size > 100 {
		fail(c, 400, 40001, "invalid pagination")
		return 0, 0, false
	}
	return p, size, true
}
func id(c *gin.Context) (int64, bool) {
	v, err := strconv.ParseInt(c.Param("id"), 10, 64)
	if err != nil || v < 1 {
		fail(c, 400, 40001, "invalid id")
		return 0, false
	}
	return v, true
}
func list(data any, p, size int, total int64) gin.H {
	return gin.H{"list": data, "page": p, "page_size": size, "total": total}
}
func (s *Server) authorize(c *gin.Context) {
	header := c.GetHeader("Authorization")
	if !strings.HasPrefix(header, "Bearer ") {
		fail(c, 401, 40101, "missing token")
		return
	}
	token, err := jwt.Parse(strings.TrimPrefix(header, "Bearer "), func(t *jwt.Token) (any, error) {
		if t.Method.Alg() != jwt.SigningMethodHS256.Alg() {
			return nil, errors.New("unexpected signing method")
		}
		return []byte(s.cfg.JWTSecret), nil
	})
	if err != nil || !token.Valid {
		fail(c, 401, 40101, "invalid or expired token")
		return
	}
	claims, valid := token.Claims.(jwt.MapClaims)
	if !valid {
		fail(c, 401, 40101, "invalid token")
		return
	}
	userID, err := strconv.ParseInt(fmt.Sprint(claims["sub"]), 10, 64)
	if err != nil {
		fail(c, 401, 40101, "invalid token subject")
		return
	}
	c.Set("user_id", userID)
	c.Next()
}
func (s *Server) sign(user model.User) (string, error) {
	return jwt.NewWithClaims(jwt.SigningMethodHS256, jwt.MapClaims{"sub": strconv.FormatInt(user.ID, 10), "role": user.Role, "exp": time.Now().Add(config.TokenTTL).Unix(), "iat": time.Now().Unix()}).SignedString([]byte(s.cfg.JWTSecret))
}
func randomTicket() (string, error) {
	b := make([]byte, 24)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return hex.EncodeToString(b), nil
}
