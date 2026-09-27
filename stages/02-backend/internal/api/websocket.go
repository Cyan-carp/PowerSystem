package api

import (
	"encoding/json"
	"net/http"
	"net/url"
	"strconv"
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
	if origin != "" {
		parsed, parseErr := url.Parse(origin)
		if parseErr != nil || parsed.Host != c.Request.Host {
			fail(c, 403, 40301, "origin not allowed")
			return
		}
	}
	upgrade := websocket.Upgrader{CheckOrigin: func(_ *http.Request) bool { return true }}
	conn, err := upgrade.Upgrade(c.Writer, c.Request, nil)
	if err != nil {
		return
	}
	s.clientsMu.Lock()
	s.clients[conn] = struct{}{}
	s.clientsMu.Unlock()
	defer func() { s.clientsMu.Lock(); delete(s.clients, conn); s.clientsMu.Unlock(); conn.Close() }()
	conn.SetReadLimit(1024)
	for {
		if _, _, err := conn.ReadMessage(); err != nil {
			return
		}
	}
}
func (s *Server) broadcast(event any) {
	payload, err := json.Marshal(event)
	if err != nil {
		return
	}
	s.clientsMu.Lock()
	defer s.clientsMu.Unlock()
	for conn := range s.clients {
		_ = conn.SetWriteDeadline(time.Now().Add(2 * time.Second))
		if err := conn.WriteMessage(websocket.TextMessage, payload); err != nil {
			delete(s.clients, conn)
			conn.Close()
		}
	}
}
