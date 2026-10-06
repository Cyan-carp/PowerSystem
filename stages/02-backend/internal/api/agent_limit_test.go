package api

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/redis/go-redis/v9"
	"powersystem/backend/internal/config"
)

type recordedChatGate struct {
	retry              int
	err                error
	acquired, released atomic.Int32
}

func (g *recordedChatGate) acquire(context.Context, int64) (chatLease, int, error) {
	g.acquired.Add(1)
	return chatLease{}, g.retry, g.err
}
func (g *recordedChatGate) release(context.Context, chatLease) error { g.released.Add(1); return nil }

func TestChatAdmissionBeforeUpstream(t *testing.T) {
	for _, tc := range []struct {
		name, body   string
		retry        int
		err          error
		want         int
		acquisitions int32
	}{
		{"rate", `{"message":"query"}`, 12, nil, 429, 1},
		{"redis", `{"message":"query"}`, 0, errors.New("private redis error"), 503, 1},
		{"invalid", `{"message":"query","user_id":3}`, 0, nil, 400, 0},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var upstreamCalls atomic.Int32
			upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { upstreamCalls.Add(1) }))
			defer upstream.Close()
			gate := &recordedChatGate{retry: tc.retry, err: tc.err}
			s := &Server{cfg: config.Config{AgentEnabled: true, AgentToken: "private", AgentURL: upstream.URL}, chatGate: gate}
			w := httptest.NewRecorder()
			agentRouter(s).ServeHTTP(w, httptest.NewRequest("POST", "/agent", strings.NewReader(tc.body)))
			if w.Code != tc.want || upstreamCalls.Load() != 0 || gate.acquired.Load() != tc.acquisitions || gate.released.Load() != 0 {
				t.Fatalf("code=%d upstream=%d acquired=%d released=%d", w.Code, upstreamCalls.Load(), gate.acquired.Load(), gate.released.Load())
			}
			if tc.retry > 0 && w.Header().Get("Retry-After") != "12" {
				t.Fatal("retry header missing")
			}
			if strings.Contains(w.Body.String(), "private redis") {
				t.Fatal("private error leaked")
			}
		})
	}
}

func TestChatLeaseReleasedOnFailureAndCancel(t *testing.T) {
	for _, cancelled := range []bool{false, true} {
		gate := &recordedChatGate{}
		entered := make(chan struct{})
		upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			close(entered)
			if cancelled {
				select {
				case <-r.Context().Done():
				case <-time.After(time.Second):
				}
				return
			}
			w.WriteHeader(503)
		}))
		s := &Server{cfg: config.Config{AgentEnabled: true, AgentToken: "private", AgentURL: upstream.URL}, chatGate: gate}
		ctx, cancel := context.WithCancel(context.Background())
		req := httptest.NewRequest("POST", "/agent", strings.NewReader(`{"message":"query"}`)).WithContext(ctx)
		w := httptest.NewRecorder()
		done := make(chan struct{})
		go func() { agentRouter(s).ServeHTTP(w, req); close(done) }()
		<-entered
		if cancelled {
			cancel()
		}
		select {
		case <-done:
		case <-time.After(2 * time.Second):
			t.Fatal("request not released")
		}
		cancel()
		upstream.Close()
		if gate.released.Load() != 1 || w.Code != 503 {
			t.Fatalf("released=%d code=%d", gate.released.Load(), w.Code)
		}
	}
}

func TestRedisChatAdmissionFailClosed(t *testing.T) {
	_, _, err := (redisChatAdmission{}).acquire(context.Background(), 17)
	if err == nil {
		t.Fatal("missing Redis must fail closed")
	}
}

// Run only against the explicitly created, disposable loopback Redis. Never
// clean a developer's existing Redis or a server's production admission keys.
func TestRedisChatAdmissionIntegration(t *testing.T) {
	url := os.Getenv("V2M4_TEST_REDIS_URL")
	if url == "" {
		t.Skip("explicit disposable Redis required")
	}
	opts, err := redis.ParseURL(url)
	if err != nil || opts.Addr != "127.0.0.1:16379" {
		t.Fatal("only disposable loopback :16379 accepted")
	}
	r := redis.NewClient(opts)
	defer r.Close()
	ctx := context.Background()
	if err := r.Ping(ctx).Err(); err != nil {
		t.Fatal(err)
	}
	clean := func() {
		keys := []string{"stage7:chat:leases"}
		for i := int64(1); i <= 20; i++ {
			keys = append(keys, chatKeys(strconv.FormatInt(i, 10))[1:]...)
		}
		if err := r.Del(ctx, keys...).Err(); err != nil {
			t.Fatal(err)
		}
	}
	clean()
	defer clean()
	g := redisChatAdmission{r}
	restart := redisChatAdmission{r}
	first, retry, err := g.acquire(ctx, 1)
	if err != nil || retry != 0 {
		t.Fatal("first admission", retry, err)
	}
	_, retry, err = restart.acquire(ctx, 1)
	if err != nil || retry <= 0 {
		t.Fatal("new session or API node bypassed user lock")
	}
	if err := g.release(ctx, first); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 9; i++ {
		lease, retry, err := restart.acquire(ctx, 1)
		if err != nil || retry != 0 {
			t.Fatal(i, retry, err)
		}
		if err := restart.release(ctx, lease); err != nil {
			t.Fatal(err)
		}
	}
	_, retry, err = g.acquire(ctx, 1)
	if err != nil || retry <= 0 {
		t.Fatal("eleventh admission not denied")
	}
	if count := r.ZCard(ctx, chatKeys("1")[2]).Val(); count != 10 {
		t.Fatal("denied request counted", count)
	}
	// Old admitted requests fall outside the rolling window, without waiting a minute.
	for _, token := range r.ZRange(ctx, chatKeys("1")[2], 0, -1).Val() {
		r.ZAdd(ctx, chatKeys("1")[2], redis.Z{Score: float64(time.Now().Add(-61 * time.Second).UnixMilli()), Member: token})
	}
	lease, retry, err := g.acquire(ctx, 1)
	if err != nil || retry != 0 {
		t.Fatal("rate window did not recover")
	}
	g.release(ctx, lease)
	clean()
	leases := []chatLease{}
	for i := int64(1); i <= 4; i++ {
		lease, retry, err := g.acquire(ctx, i)
		if err != nil || retry != 0 {
			t.Fatal("global setup", i, retry, err)
		}
		leases = append(leases, lease)
	}
	_, retry, err = restart.acquire(ctx, 5)
	if err != nil || retry <= 0 {
		t.Fatal("global fifth request admitted")
	}
	g.release(ctx, leases[0])
	replacement, retry, err := restart.acquire(ctx, 5)
	if err != nil || retry != 0 {
		t.Fatal("global lease not released")
	}
	// A late release cannot remove another request's per-user lease.
	r.Set(ctx, chatKeys("5")[1], "new-token", 50*time.Second)
	g.release(ctx, replacement)
	if r.Get(ctx, chatKeys("5")[1]).Val() != "new-token" {
		t.Fatal("late release deleted newer lease")
	}
	clean()
	r.ZAdd(ctx, "stage7:chat:leases", redis.Z{Score: float64(time.Now().Add(-time.Second).UnixMilli()), Member: "crashed"})
	r.Set(ctx, chatKeys("1")[1], "crashed", 10*time.Millisecond)
	time.Sleep(20 * time.Millisecond)
	lease, retry, err = restart.acquire(ctx, 1)
	if err != nil || retry != 0 {
		t.Fatal("expired crash lease did not recover")
	}
	if ttl := r.PTTL(ctx, chatKeys("1")[1]).Val(); ttl < 49*time.Second || ttl > 50*time.Second {
		t.Fatal("lease not 50 seconds", ttl)
	}
	g.release(ctx, lease)
	clean()
	// Simultaneous users must still admit exactly four. Sequential capacity
	// checks alone would not expose an accidental read-then-write race.
	type admissionResult struct {
		lease chatLease
		retry int
		err   error
	}
	start := make(chan struct{})
	results := make(chan admissionResult, 20)
	for i := int64(1); i <= 20; i++ {
		go func(user int64) {
			<-start
			lease, retry, err := g.acquire(ctx, user)
			results <- admissionResult{lease, retry, err}
		}(i)
	}
	close(start)
	admitted := []chatLease{}
	for i := 0; i < 20; i++ {
		result := <-results
		if result.err != nil {
			t.Fatal(result.err)
		}
		if result.retry == 0 {
			admitted = append(admitted, result.lease)
		}
	}
	if len(admitted) != 4 {
		t.Fatalf("concurrent admissions=%d", len(admitted))
	}
	for _, lease := range admitted {
		g.release(ctx, lease)
	}
	clean()
	for _, deadline := range []bool{false, true} {
		entered := make(chan struct{})
		upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
			close(entered)
			select {
			case <-req.Context().Done():
			case <-time.After(time.Second):
			}
		}))
		s := &Server{cfg: config.Config{AgentEnabled: true, AgentToken: "private", AgentURL: upstream.URL}, chatGate: g}
		requestCtx, cancel := context.WithCancel(ctx)
		if deadline {
			requestCtx, cancel = context.WithTimeout(ctx, 200*time.Millisecond)
		}
		w := httptest.NewRecorder()
		req := httptest.NewRequest("POST", "/agent", strings.NewReader(`{"message":"query"}`)).WithContext(requestCtx)
		done := make(chan struct{})
		go func() { agentRouter(s).ServeHTTP(w, req); close(done) }()
		select {
		case <-entered:
		case <-time.After(2 * time.Second):
			cancel()
			upstream.Close()
			t.Fatal("upstream not entered")
		}
		if r.Exists(ctx, chatKeys("17")[1]).Val() != 1 {
			t.Fatal("live request lease absent")
		}
		if !deadline {
			cancel()
		}
		select {
		case <-done:
		case <-time.After(2 * time.Second):
			t.Fatal("cancel/deadline lease not released")
		}
		cancel()
		upstream.Close()
		if r.Exists(ctx, chatKeys("17")[1]).Val() != 0 || r.ZCard(ctx, "stage7:chat:leases").Val() != 0 {
			t.Fatal("cancel/deadline retained real Redis lease")
		}
	}
}
