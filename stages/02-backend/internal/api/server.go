package api

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"net"
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
	"powersystem/backend/internal/energy"
	"powersystem/backend/internal/fleet"
	"powersystem/backend/internal/model"
	"powersystem/backend/internal/service"
	"powersystem/backend/internal/tdengine"
	"powersystem/backend/internal/telemetry"
)

type Server struct {
	ingressOnce  sync.Once
	ingressQueue chan mqtt.Message
	ingressDone  chan struct{}

	cfg          config.Config
	db           *gorm.DB
	redis        *redis.Client
	td           *tdengine.Client
	log          *zap.Logger
	router       *gin.Engine
	clients      map[*websocket.Conn]*realtimeClient
	clientsMu    sync.Mutex
	chatGate     chatAdmission
	realtime     realtimeHub
	startupMu    sync.Mutex
	startupPhase string
	restoreOK    bool
	runContext   context.Context

	telemetry         telemetryMonitor
	scheduler         inboxScheduler
	snapshotMu        sync.Mutex
	tdCircuit         dependencyCircuit
	snapshotTDCircuit dependencyCircuit
	redisCircuit      dependencyCircuit

	fleetCatalog *fleet.Catalog
}

func New(cfg config.Config, db *gorm.DB, redis *redis.Client, td *tdengine.Client, log *zap.Logger) *Server {
	gin.SetMode(gin.ReleaseMode)
	s := &Server{cfg: cfg, db: db, redis: redis, td: td, log: log, clients: make(map[*websocket.Conn]*realtimeClient), startupPhase: "created"}
	s.telemetry.touch("start", time.Now())
	// The fleet manifest is optional: a missing or invalid file must not stop the
	// existing API from serving devices and alarms without scope filtering.
	if cfg.FleetPath != "" {
		catalog, err := fleet.Load(cfg.FleetPath)
		if err != nil {
			if log != nil {
				log.Warn("fleet_config_unavailable", zap.String("path", cfg.FleetPath), zap.Error(err))
			}
		} else {
			s.fleetCatalog = catalog
		}
	}
	r := gin.New()
	r.Use(gin.Recovery())
	r.Use(httpMetrics.observe())
	r.GET("/metrics", func(c *gin.Context) { s.serveTelemetryMetrics(c) })
	r.GET("/api/v1/ping", func(c *gin.Context) {
		ready, phase, _ := s.readinessSnapshot()
		if s.runContext != nil && !ready {
			fail(c, 503, 50301, "telemetry not ready: "+phase)
			return
		}
		ok(c, gin.H{"status": "ok", "telemetry_ready": ready, "telemetry_phase": phase})
	})
	r.GET("/live", func(c *gin.Context) { ok(c, gin.H{"status": "alive"}) })
	r.GET("/ready", s.telemetryReady)
	r.POST("/api/v1/auth/register", s.register)
	r.POST("/api/v1/auth/login", s.login)
	a := r.Group("/api/v1", s.authorize)
	a.Use(func(c *gin.Context) {
		if s.runContext != nil {
			s.startupMu.Lock()
			restored := s.restoreOK
			s.startupMu.Unlock()
			if !restored {
				fail(c, 503, 50301, "telemetry startup recovery incomplete")
			}
		}
	})
	a.Use(s.alarmMaintenanceGuard)
	a.GET("/devices", s.listDevices)
	a.GET("/stations", s.listStations)
	a.POST("/devices", requireAdmin, s.createDevice)
	a.GET("/devices/:id", s.getDevice)
	a.PUT("/devices/:id", requireAdmin, s.updateDevice)
	a.DELETE("/devices/:id", requireAdmin, s.deleteDevice)
	a.GET("/devices/:id/telemetry", s.history)
	a.GET("/devices/:id/telemetry/latest", s.latest)
	a.GET("/devices/:id/telemetry/progress", s.telemetryProgress)
	a.GET("/devices/:id/telemetry/export", s.exportTelemetry)
	a.GET("/devices/:id/prediction", s.getPrediction)
	a.GET("/predictions", s.listPredictions)
	a.GET("/alarm-rules", s.listRules)
	a.POST("/alarm-rules", requireAdmin, s.createRule)
	a.GET("/alarm-rules/:id", s.getRule)
	a.PUT("/alarm-rules/:id", requireAdmin, s.updateRule)
	a.DELETE("/alarm-rules/:id", requireAdmin, s.deleteRule)
	a.GET("/alarms", s.listAlarms)
	a.GET("/alarms/statistics", s.alarmStatistics)
	a.GET("/alarms/:id", s.getAlarm)
	a.POST("/alarms/:id/ack", s.ackAlarm)
	a.POST("/alarms/:id/archive", s.archiveAlarm)
	a.POST("/alarm-archive", s.archiveAlarms)
	a.GET("/health/devices", s.healthDevices)
	a.GET("/health/stations", s.healthStations)
	a.GET("/reports/availability", s.availability)
	a.GET("/reports/alarm-timeliness", s.alarmTimeliness)
	a.GET("/reports/energy", s.energyReport)
	a.GET("/agent/artifacts/:id", s.agentArtifactsProxy)
	a.GET("/agent/artifacts/:id/download", s.agentArtifactsProxy)
	a.GET("/dashboard/summary", s.dashboard)
	a.GET("/dashboard/overview", s.dashboardOverview)
	a.POST("/ws-ticket", s.ticket)
	a.POST("/agent/chat", s.agentChat)
	a.POST("/agent/runs", s.agentRunsProxy)
	a.GET("/agent/runs/:id", s.agentRunsProxy)
	a.GET("/agent/runs/:id/events", s.agentRunsProxy)
	a.POST("/agent/runs/:id/cancel", s.agentRunsProxy)
	a.POST("/agent/runs/:id/actions/:action_id/decision", s.agentActionDecision)
	a.GET("/agent/schedules", s.agentSchedulesProxy)
	a.POST("/agent/schedules", s.agentSchedulesProxy)
	a.GET("/agent/schedules/:id", s.agentSchedulesProxy)
	a.PUT("/agent/schedules/:id", s.agentSchedulesProxy)
	a.GET("/agent/schedules/:id/history", s.agentSchedulesProxy)
	a.POST("/agent/schedules/:id/pause", s.agentSchedulesProxy)
	a.POST("/agent/schedules/:id/cancel", s.agentSchedulesProxy)
	a.POST("/agent/schedules/:id/resume", s.agentSchedulesProxy)
	a.POST("/agent/rule-preview", s.agentSchedulesProxy)
	r.GET("/internal/agent/schedule-read/:id", s.scheduleRead)
	a.PATCH("/devices/:id/remark", s.executeBusinessAction)
	a.GET("/agent/tasks", s.opsProxy)
	a.GET("/agent/tasks/:id", s.opsProxy)
	a.POST("/agent/tasks/:id/cancel", s.opsProxy)
	a.DELETE("/agent/tasks/:id", s.opsProxy)
	a.POST("/agent/tasks/:id/approvals/:approval_id/decision", s.opsProxy)
	r.GET("/internal/ma/v1/actor", s.maActor)
	r.POST("/internal/ma-action/v1/snapshot", s.authorize, s.businessActionSnapshot)
	r.POST("/internal/ma-action/v1/execute", s.authorize, s.executeBusinessAction)
	r.GET("/internal/ma-action/v1/receipts/:execution_ref", s.authorize, s.businessExecutionReceipt)
	a.GET("/agent/sessions", s.agentRAGProxy)
	a.GET("/agent/sessions/:id", s.agentRAGProxy)
	a.POST("/agent/sessions/:id/verify", s.agentRAGVerify)
	a.DELETE("/agent/sessions/:id", s.agentRAGProxy)
	a.GET("/agent/rag/sources/:id", s.agentRAGProxy)
	a.GET("/agent/knowledge/:id", s.agentKnowledge)
	a.GET("/agent/interpretations", s.listInterpretations)
	a.GET("/agent/interpretations/:id", s.getInterpretation)
	a.POST("/agent/interpretations/:id/read", s.readInterpretation)
	a.GET("/agent/model-status", requireAdmin, s.agentModelStatus)
	a.POST("/agent/model-probe", requireAdmin, s.agentModelProbe)
	r.GET("/ws/realtime", s.websocket)
	r.GET("/test/ws", func(c *gin.Context) { c.File("test/ws.html") })
	s.router = r
	return s
}
func (s *Server) Run(parent context.Context) (runErr error) {
	ctx, stop := signal.NotifyContext(parent, os.Interrupt, syscall.SIGTERM)
	defer stop()
	s.runContext = ctx
	s.ensureRealtime()
	var wg sync.WaitGroup
	launch := func(fn func(context.Context)) { wg.Add(1); go func() { defer wg.Done(); fn(ctx) }() }
	httpServer := &http.Server{Addr: s.cfg.HTTPAddr, Handler: s.router, ReadHeaderTimeout: 5 * time.Second, BaseContext: func(_ net.Listener) context.Context { return ctx }}
	errCh := make(chan error, 1)
	go func() { errCh <- httpServer.ListenAndServe() }()
	defer func() {
		s.setStartup("stopping", false)
		stop()
		s.realtime.cancel()
		shut, cancel := context.WithTimeout(context.Background(), shutdownBudget)
		defer cancel()
		if err := httpServer.Shutdown(shut); err != nil {
			httpServer.Close()
			runErr = err
		}
		done := make(chan struct{})
		go func() { wg.Wait(); close(done) }()
		select {
		case <-done:
		case <-shut.Done():
			runErr = errors.New("telemetry_shutdown_timeout")
		}
		if s.ingressDone != nil {
			select {
			case <-s.ingressDone:
			case <-shut.Done():
				runErr = errors.New("ingress_shutdown_timeout")
			}
		}
		select {
		case <-s.realtime.done:
		case <-shut.Done():
			runErr = errors.New("realtime_shutdown_timeout")
		}
	}()
	launch(s.telemetryWatchdog)
	s.setStartup("subscribing", false)
	if err := s.subscribe(ctx); err != nil {
		return err
	}
	launch(func(ctx context.Context) {
		for ctx.Err() == nil {
			s.setStartup("restoring", false)
			if err := s.restoreLatest(ctx); err == nil {
				s.setStartup("running", true)
				launch(s.worker)
				launch(s.snapshotWorker)
				launch(s.energyWorker)
				if s.cfg.SimulationAlarmMode != "off" && s.cfg.SimulationAlarmMode != "" {
					launch(s.simulationAlarmWorker)
				}
				if s.cfg.AgentInterpretEnabled {
					launch(s.interpretationScanner)
					launch(s.monitorConsumer)
					for i := 0; i < 2; i++ {
						launch(s.interpretationWorker)
					}
				}
				if s.cfg.BusinessNotifyEnabled {
					launch(s.businessNotificationWorker)
				}
				if s.cfg.AIEnabled {
					launch(s.predictionWorker)
				}
				return
			}
			s.setStartup("restore_failed", false)
			s.telemetryLog("restore_failed", time.Now())
			timer := time.NewTimer(5 * time.Second)
			select {
			case <-ctx.Done():
				timer.Stop()
				return
			case <-timer.C:
			}
		}
	})
	s.log.Info("api_started", zap.String("address", s.cfg.HTTPAddr))
	select {
	case <-ctx.Done():
		return nil
	case err := <-errCh:
		if err == http.ErrServerClosed {
			return nil
		}
		return err
	}
}

// Handler is used by the isolated local MA acceptance proxy without starting business workers.
func (s *Server) Handler() http.Handler { return s.router }
func (s *Server) subscribe(ctx context.Context) error {
	s.ingressOnce.Do(func() {
		s.ingressQueue = make(chan mqtt.Message, 128)
		s.ingressDone = make(chan struct{})
		var receivers sync.WaitGroup
		for n := 0; n < 4; n++ {
			receivers.Add(1)
			go func() {
				defer receivers.Done()
				for {
					select {
					case <-ctx.Done():
						return
					case message := <-s.ingressQueue:
						for !s.receive(nil, message) {
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
			}()
		}
		go func() { receivers.Wait(); close(s.ingressDone) }()
	})
	storePath := s.cfg.QueuePath + ".mqtt-store"
	if err := os.MkdirAll(filepath.Dir(storePath), 0755); err != nil {
		return err
	}
	if err := os.MkdirAll(storePath, 0755); err != nil {
		return err
	}
	opts := mqtt.NewClientOptions().AddBroker(s.cfg.Broker).SetClientID(s.cfg.ClientID).SetCleanSession(false).SetStore(mqtt.NewFileStore(storePath)).SetAutoReconnect(true).SetConnectRetry(true).SetConnectRetryInterval(time.Second).SetMaxReconnectInterval(30 * time.Second).SetAutoAckDisabled(true).SetOrderMatters(true)
	opts.SetOnConnectHandler(func(c mqtt.Client) {
		token := c.Subscribe("device/telemetry", 1, s.acceptTelemetry)
		wait, finish := context.WithTimeout(ctx, 2*time.Second)
		defer finish()
		select {
		case <-token.Done():
		case <-wait.Done():
		}
		if ctx.Err() != nil {
			return
		}
		if wait.Err() != nil || token.Error() != nil {
			s.log.Error("backend_subscribe_failed", zap.Error(token.Error()))
		}
	})
	client := mqtt.NewClient(opts)
	client.AddRoute("device/telemetry", s.acceptTelemetry)
	token := client.Connect()
	connect, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	select {
	case <-token.Done():
	case <-connect.Done():
		client.Disconnect(0)
		return connect.Err()
	}
	if token.Error() != nil {
		return fmt.Errorf("mqtt connect: %v", token.Error())
	}
	go func() { <-ctx.Done(); client.Disconnect(500) }()
	return nil
}

// The bounded volatile admission buffer owns no queue truth: messages remain
// unacknowledged until the database commits, and QoS1 owns crash redelivery.
func (s *Server) acceptTelemetry(_ mqtt.Client, m mqtt.Message) {
	if len(m.Payload()) > 64*1024 {
		m.Ack()
		return
	}
	select {
	case <-s.runContext.Done():
		return
	case s.ingressQueue <- m:
	}
}
func (s *Server) receive(_ mqtt.Client, message mqtt.Message) bool {
	measured := time.Now()
	defer func() { s.telemetry.elapsed("ingress", time.Since(measured)) }()
	parent := s.runContext
	if parent == nil {
		parent = context.Background()
	}
	ctx, cancel := context.WithTimeout(parent, inboxTransactionBudget)
	defer cancel()
	db := s.db.WithContext(ctx)
	sample, err := telemetry.Parse(message.Payload())
	if err != nil {
		s.log.Warn("backend_rejected", zap.Error(err))
		message.Ack()
		return true
	}
	var device model.Device
	if err = db.Where("device_code = ? AND deleted_at IS NULL", sample.DeviceCode).First(&device).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			message.Ack()
		} else {
			s.log.Error("inbox_device_lookup", zap.Error(err))
		}
		return errors.Is(err, gorm.ErrRecordNotFound)
	}
	if device.StationCode != sample.StationCode {
		message.Ack()
		return true
	}
	var admission struct {
		GateCount int64
		Admitted  int64
	}
	// One server-side statement is one transaction. MATERIALIZED gate and device
	// lock keep the same drain/ordering protocol while avoiding seven RTTs per
	// serial MQTT callback. A lost acknowledgement is safely retried by identity.
	err = db.Raw(`WITH gate AS MATERIALIZED (SELECT enabled FROM alarm_maintenance_state WHERE id=1 FOR SHARE),
 locked AS MATERIALIZED (SELECT pg_advisory_xact_lock(hashtextextended(?,0)) FROM gate WHERE NOT enabled),
 inserted AS (INSERT INTO telemetry_inbox(run_id,device_id,seq,ts_ms,payload,received_at)
 SELECT ?,?,?,?,?::jsonb,clock_timestamp() FROM locked ON CONFLICT(run_id,device_id,seq) DO NOTHING RETURNING device_id,ts_ms),
 dirty AS (INSERT INTO energy_dirty_days(device_id,day,changed_at)
 SELECT DISTINCT device_id,(to_timestamp((ts_ms+offset_ms)/1000.0) AT TIME ZONE 'Asia/Shanghai')::date,statement_timestamp()
 FROM inserted CROSS JOIN (VALUES(-15000::bigint),(0::bigint),(15000::bigint)) offsets(offset_ms)
 ON CONFLICT(device_id,day) DO UPDATE SET changed_at=excluded.changed_at RETURNING device_id)
 SELECT (SELECT count(*) FROM gate) AS gate_count,(SELECT count(*) FROM locked) AS admitted`, fmt.Sprintf("telemetry-device:%d", device.ID), sample.RunID, device.ID, sample.Seq, sample.TS, string(message.Payload())).Scan(&admission).Error
	if err == nil && (admission.GateCount != 1 || admission.Admitted != 1) {
		return false
	}

	if err != nil {
		s.log.Error("inbox_insert_failed", zap.Error(err))
		return false
	}
	message.Ack()
	return true
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

// S02 budgets remain unchanged. S03 bounds selection, retry and dependency work.
const inboxTickBudget = 30 * time.Second
const inboxItemBudget = 5 * time.Second
const inboxTransactionBudget = 3 * time.Second
const telemetryDependencyBudget = time.Second
const restoreBudget = 30 * time.Second
const shutdownBudget = 5 * time.Second

func (s *Server) processInboxItem(parent context.Context, item model.Inbox) {
	measured := time.Now()
	defer func() { s.telemetry.elapsed("history", time.Since(measured)) }()
	ctx, cancel := context.WithTimeout(parent, inboxItemBudget)
	defer cancel()
	txctx, txcancel := context.WithTimeout(ctx, inboxTransactionBudget)
	defer txcancel()
	code := "business_failed"
	quarantine := false
	var events []service.AlarmEvent
	err := service.TelemetryTransaction(s.db.WithContext(txctx), func(tx *gorm.DB) error {
		// Resolve an indeterminate commit before Redis or business side effects.
		if err := service.TelemetryDeviceLock(tx, item.DeviceID); err != nil {
			return err
		}
		var current model.Inbox
		if err := tx.Raw("SELECT * FROM telemetry_inbox WHERE id=? FOR UPDATE", item.ID).Scan(&current).Error; err != nil {
			return err
		}
		if current.ID == 0 || current.ProcessedAt != nil {
			return service.ErrInboxTerminal
		}
		item = current
		if current.NextAttemptAt != nil && current.NextAttemptAt.After(time.Now()) {
			return errInboxDeferred
		}
		var head struct{ ID int64 }
		if err := tx.Raw("SELECT id FROM telemetry_inbox WHERE device_id=? AND processed_at IS NULL ORDER BY id LIMIT 1", item.DeviceID).Scan(&head).Error; err != nil {
			return err
		}
		if head.ID != item.ID {
			return errInboxDeferred
		}
		sample, parseErr := telemetry.ParseStored(item.Payload)
		if parseErr != nil || sample.SchemaVersion != 1 || sample.RunID != item.RunID || sample.Seq != item.Seq || sample.TS != item.TS {
			code, quarantine = "payload_invalid", false
			return errors.New(code)
		}
		var device model.Device
		err := tx.Where("id=? AND deleted_at IS NULL", item.DeviceID).First(&device).Error
		if errors.Is(err, gorm.ErrRecordNotFound) {
			code, quarantine = "device_deleted", true
			return err
		}
		if err != nil {
			code = "device_lookup"
			return err
		}
		if sample.DeviceCode != device.DeviceCode || sample.StationCode != device.StationCode {
			code, quarantine = "identity_invalid", true
			return errors.New(code)
		}
		if item.TDConfirmedAt == nil {
			if !s.tdCircuit.enter(time.Now()) {
				s.telemetry.touch("td_circuit_skip", time.Now())
				return errDependencyOpen
			}
			dep, depcancel := context.WithTimeout(txctx, telemetryDependencyBudget)
			found, err := s.td.HasSample(dep, item.DeviceID, item.TS, item.Seq)
			depcancel()
			s.tdCircuit.finish(time.Now(), err, txctx.Err() != nil)
			s.telemetry.touch("td_circuit_attempt", time.Now())
			if err != nil {
				code = "td_unavailable"
				return err
			}
			if !found {
				code = "td_missing"
				return errors.New(code)
			}
			if err := tx.Model(&model.Inbox{}).Where("id=?", item.ID).Update("td_confirmed_at", gorm.Expr("clock_timestamp()")).Error; err != nil {
				return err
			}
		}

		businessMeasured := time.Now()
		events, err = service.ProcessLockedSample(tx, item, sample, device)
		s.telemetry.elapsed("business_pg", time.Since(businessMeasured))
		return err
	})
	if err != nil {
		switch {
		case errors.Is(err, service.ErrCommitUnknown):
			s.requestRealtimeRefresh()
			s.telemetry.touch("commit_unknown", time.Now())
			s.telemetryLog("commit_unknown", time.Now())
		case errors.Is(err, service.ErrInboxTerminal), errors.Is(err, errInboxDeferred), errors.Is(err, errDependencyOpen):
		case errors.Is(err, service.ErrAlarmMaintenance), errors.Is(err, service.ErrMaintenanceMissing):
			s.telemetryLog("maintenance_transaction", time.Now())
		default:
			if ctx.Err() != nil || txctx.Err() != nil {
				s.telemetry.touch("cancelled", time.Now())
				s.telemetryLog("cancelled", time.Now())
				return
			}
			s.recordInboxFailure(ctx, item, code, quarantine)
		}
		return
	}
	s.telemetry.touch("success", time.Now())
	s.telemetry.touch("advance", time.Now())
	for _, event := range events {
		s.broadcast(event)
	}
}

func (s *Server) cacheLatest(ctx context.Context, device model.Device, sample telemetry.Sample, receivedAt time.Time) (gin.H, error) {
	var item model.Inbox
	if err := s.db.WithContext(ctx).Where("device_id=? AND run_id=? AND seq=? AND ts_ms=? AND td_confirmed_at IS NOT NULL", device.ID, sample.RunID, sample.Seq, sample.TS).First(&item).Error; err != nil {
		return nil, err
	}
	if err := s.refreshDeviceSnapshot(ctx, device); err != nil {
		return nil, err
	}
	return gin.H{"snapshot_source": "td_confirmed", "business_commit": "not_asserted"}, nil
}

// Startup and runtime recovery use the same bounded durable-source path.
func (s *Server) restoreLatest(ctx context.Context) error {
	return s.restoreSnapshots(ctx)
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
	if err != nil || userID < 1 {
		fail(c, 401, 40101, "invalid token subject")
		return
	}
	var user model.User
	if err := s.db.Select("id", "role").First(&user, userID).Error; err != nil || (user.Role != "admin" && user.Role != "operator") {
		fail(c, 401, 40101, "user unavailable")
		return
	}
	c.Set("user_id", userID)
	c.Set("user_role", user.Role)
	c.Next()
}
func (s *Server) energyWorker(ctx context.Context) {
	ticker := time.NewTicker(10 * time.Second)
	defer ticker.Stop()
	for {
		round, finish := context.WithTimeout(ctx, inboxTransactionBudget)
		_, err := energy.RebuildPending(round, s.db, 12)
		finish()
		if err != nil && ctx.Err() == nil {
			s.log.Warn("energy_rebuild_failed", zap.Error(err))
		}
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}
func requireAdmin(c *gin.Context) {
	if role, ok := c.Get("user_role"); !ok || role != "admin" {
		fail(c, 403, 40301, "admin role required")
		return
	}
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
