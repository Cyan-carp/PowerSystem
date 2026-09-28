package api

import (
	"fmt"
	"net/http"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/gin-gonic/gin"
)

type metricKey struct {
	method string
	route  string
	status int
}

type requestMetric struct {
	count   int64
	seconds float64
}

type metricRegistry struct {
	mu       sync.Mutex
	requests map[metricKey]requestMetric
}

var httpMetrics = &metricRegistry{requests: make(map[metricKey]requestMetric)}

func (m *metricRegistry) observe() gin.HandlerFunc {
	return func(c *gin.Context) {
		start := time.Now()
		c.Next()
		route := c.FullPath()
		if route == "" {
			route = "unmatched"
		}
		if route == "/metrics" {
			return
		}
		key := metricKey{method: c.Request.Method, route: route, status: c.Writer.Status()}
		m.mu.Lock()
		entry := m.requests[key]
		entry.count++
		entry.seconds += time.Since(start).Seconds()
		m.requests[key] = entry
		m.mu.Unlock()
	}
}

func (m *metricRegistry) serve(c *gin.Context) {
	m.mu.Lock()
	keys := make([]metricKey, 0, len(m.requests))
	for key := range m.requests {
		keys = append(keys, key)
	}
	sort.Slice(keys, func(i, j int) bool {
		a, b := keys[i], keys[j]
		if a.route != b.route {
			return a.route < b.route
		}
		if a.method != b.method {
			return a.method < b.method
		}
		return a.status < b.status
	})
	var body strings.Builder
	body.WriteString("# HELP powersystem_http_requests_total Total HTTP requests.\n# TYPE powersystem_http_requests_total counter\n")
	for _, key := range keys {
		value := m.requests[key]
		fmt.Fprintf(&body, "powersystem_http_requests_total{method=%q,route=%q,status=%q} %d\n", key.method, key.route, fmt.Sprint(key.status), value.count)
	}
	body.WriteString("# HELP powersystem_http_request_duration_seconds_sum Total HTTP request duration in seconds.\n# TYPE powersystem_http_request_duration_seconds_sum counter\n")
	for _, key := range keys {
		value := m.requests[key]
		fmt.Fprintf(&body, "powersystem_http_request_duration_seconds_sum{method=%q,route=%q,status=%q} %g\n", key.method, key.route, fmt.Sprint(key.status), value.seconds)
	}
	m.mu.Unlock()
	c.Data(http.StatusOK, "text/plain; version=0.0.4", []byte(body.String()))
}
