package api

import (
	"context"
	"encoding/json"
	"errors"
	"net"
	"net/http"
	"strconv"
	"sync"
	"sync/atomic"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/gorilla/websocket"
)

func (s *Server) ticket(c *gin.Context) {
	ticket, err := randomTicket()
	if err != nil {
		fail(c, 500, 50000, "ticket generation failed")
		return
	}
	userID := c.GetInt64("user_id")
	if err = s.redis.Set(c.Request.Context(), "ws-ticket:"+ticket, strconv.FormatInt(userID, 10), 60*time.Second).Err(); err != nil {
		fail(c, 503, 50000, "ticket store unavailable")
		return
	}
	ok(c, gin.H{"ticket": ticket, "expires_in": 60})
}
func (s *Server) websocket(c *gin.Context) {
	ticket := c.Query("ticket")
	if len(ticket) != 48 {
		fail(c, 401, 40101, "invalid ticket")
		return
	}
	value, err := s.redis.GetDel(c.Request.Context(), "ws-ticket:"+ticket).Result()
	if err != nil || value == "" {
		fail(c, 401, 40101, "expired ticket")
		return
	}
	origin := c.GetHeader("Origin")
	if !originAllowed(origin, c.Request.Host, s.cfg.WSAllowedOrigins) {
		fail(c, 403, 40301, "origin not allowed")
		return
	}
	upgrade := websocket.Upgrader{CheckOrigin: func(_ *http.Request) bool { return true }}
	conn, err := upgrade.Upgrade(c.Writer, c.Request, nil)
	if err != nil {
		return
	}
	s.ensureRealtime()
	client := &realtimeClient{conn: conn, send: make(chan []byte, realtimeClientQueue), done: make(chan struct{})}
	s.clientsMu.Lock()
	select {
	case <-s.realtime.done:
		s.clientsMu.Unlock()
		conn.Close()
		return
	default:
	}
	if s.runContext != nil && s.runContext.Err() != nil {
		s.clientsMu.Unlock()
		conn.Close()
		return
	}
	if s.realtime.stopped || len(s.clients) >= realtimeMaxClients {
		s.clientsMu.Unlock()
		conn.Close()
		return
	}
	client.epoch = s.realtime.epoch.Load()
	s.realtime.writers.Add(2)
	s.clients[conn] = client
	s.clientsMu.Unlock()
	go s.writeRealtime(client)
	defer s.realtime.writers.Done()
	defer func() { s.clientsMu.Lock(); delete(s.clients, conn); s.clientsMu.Unlock(); client.close() }()
	conn.SetReadLimit(1024)
	for {
		if _, _, err := conn.ReadMessage(); err != nil {
			return
		}
	}
}

func originAllowed(origin, requestHost string, extra []string) bool {
	if origin == "" {
		return true
	}
	for _, allowed := range extra {
		if origin == allowed {
			return true
		}
	}
	return origin == "http://"+requestHost || origin == "https://"+requestHost
}

const realtimeQueueSize = 64
const realtimeClientQueue = 8
const realtimeMaxClients = 128
const realtimeMaxPayload = 16 << 10
const realtimeWriteBudget = time.Second

type realtimeClient struct {
	conn  *websocket.Conn
	send  chan []byte
	done  chan struct{}
	once  sync.Once
	epoch uint64
}

func (c *realtimeClient) close() { c.once.Do(func() { close(c.done); c.conn.Close() }) }

type realtimeHub struct {
	once    sync.Once
	queue   chan realtimeEvent
	reset   chan struct{}
	cancel  context.CancelFunc
	done    chan struct{}
	epoch   atomic.Uint64
	writers sync.WaitGroup
	stopped bool
}

func (s *Server) ensureRealtime() {
	s.realtime.once.Do(func() {
		ctx := s.runContext
		if ctx == nil {
			ctx = context.Background()
		}
		ctx, cancel := context.WithCancel(ctx)
		s.realtime.cancel = cancel
		s.realtime.queue = make(chan realtimeEvent, realtimeQueueSize)
		s.realtime.reset = make(chan struct{}, 1)
		s.realtime.done = make(chan struct{})
		go s.dispatchRealtime(ctx)
	})
}
func (s *Server) closeRealtimeClients() {
	s.clientsMu.Lock()
	defer s.clientsMu.Unlock()
	for conn, client := range s.clients {
		client.close()
		delete(s.clients, conn)
	}
}
func (s *Server) dispatchRealtime(ctx context.Context) {
	defer close(s.realtime.done)
	defer func() {
		s.clientsMu.Lock()
		s.realtime.stopped = true
		for conn, client := range s.clients {
			client.close()
			delete(s.clients, conn)
		}
		s.clientsMu.Unlock()
		s.realtime.writers.Wait()
	}()
	for {
		select {
		case <-ctx.Done():
			return
		case <-s.realtime.reset:
			// Lost events cannot be silently followed by newer events on the same
			// connection. Disconnect and require the existing read-only HTTP refresh.
			s.clientsMu.Lock()
			for conn, client := range s.clients {
				if client.epoch < s.realtime.epoch.Load() {
					client.close()
					delete(s.clients, conn)
				}
			}
			s.clientsMu.Unlock()
		case frame := <-s.realtime.queue:
			if frame.epoch != s.realtime.epoch.Load() {
				continue
			}
			payload := frame.payload
			s.clientsMu.Lock()
			for conn, client := range s.clients {
				select {
				case client.send <- payload:
				default:
					client.close()
					delete(s.clients, conn)
					s.telemetry.touch("ws_slow", time.Now())
				}
			}
			s.clientsMu.Unlock()
		}
	}
}
func (s *Server) writeRealtime(client *realtimeClient) {
	defer s.realtime.writers.Done()
	defer client.close()
	ticker := time.NewTicker(20 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-client.done:
			return
		case payload := <-client.send:
			client.conn.SetWriteDeadline(time.Now().Add(realtimeWriteBudget))
			if err := client.conn.WriteMessage(websocket.TextMessage, payload); err != nil {
				var timeout net.Error
				if errors.As(err, &timeout) && timeout.Timeout() {
					s.telemetry.touch("ws_write_timeout", time.Now())
				}
				return
			}
		case <-ticker.C:
			if client.conn.WriteControl(websocket.PingMessage, nil, time.Now().Add(realtimeWriteBudget)) != nil {
				return
			}
		}
	}
}
func (s *Server) broadcast(event any) {
	payload, err := json.Marshal(event)
	if err != nil {
		return
	}
	s.ensureRealtime()
	if len(payload) > realtimeMaxPayload {
		s.telemetry.touch("ws_drop", time.Now())
		s.requestRealtimeRefresh()
		return
	}
	select {
	case s.realtime.queue <- realtimeEvent{payload: payload, epoch: s.realtime.epoch.Load()}:
	default:
		s.telemetry.touch("ws_drop", time.Now())
		s.requestRealtimeRefresh()
	}
}

func (s *Server) requestRealtimeRefresh() {
	s.ensureRealtime()
	s.realtime.epoch.Add(1)
	select {
	case s.realtime.reset <- struct{}{}:
	default:
	}
}

type realtimeEvent struct {
	payload []byte
	epoch   uint64
}
