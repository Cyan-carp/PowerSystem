package api

import (
	"context"
	"errors"
	"strconv"
	"time"

	"github.com/redis/go-redis/v9"
)

// Only interactive chat shares this admission pool. Interpretation workers
// never acquire these keys. Redis TIME avoids clocks differing across API nodes.
const chatAcquireLua = `
local clock = redis.call('TIME')
local now = clock[1]*1000 + math.floor(clock[2]/1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
redis.call('ZREMRANGEBYSCORE', KEYS[3], '-inf', now-60000)
local busy = redis.call('PTTL', KEYS[2])
if busy > 0 then return math.max(1, math.ceil(busy/1000)) end
if redis.call('ZCARD', KEYS[1]) >= 4 then
  local first = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
  return math.max(1, math.ceil((tonumber(first[2])-now)/1000))
end
if redis.call('ZCARD', KEYS[3]) >= 10 then
  local first = redis.call('ZRANGE', KEYS[3], 0, 0, 'WITHSCORES')
  return math.max(1, math.ceil((tonumber(first[2])+60000-now)/1000))
end
redis.call('SET', KEYS[2], ARGV[1], 'PX', 50000)
redis.call('ZADD', KEYS[1], now+50000, ARGV[1])
redis.call('PEXPIRE', KEYS[1], 51000)
redis.call('ZADD', KEYS[3], now, ARGV[1])
redis.call('PEXPIRE', KEYS[3], 61000)
return 0
`

const chatReleaseLua = `
if redis.call('GET', KEYS[2]) == ARGV[1] then redis.call('DEL', KEYS[2]) end
return redis.call('ZREM', KEYS[1], ARGV[1])
`

type chatLease struct{ user, token string }
type chatAdmission interface {
	acquire(context.Context, int64) (chatLease, int, error)
	release(context.Context, chatLease) error
}
type redisChatAdmission struct{ client *redis.Client }

func chatKeys(user string) []string {
	return []string{"stage7:chat:leases", "stage7:chat:user:" + user, "stage7:chat:rate:" + user}
}

func (g redisChatAdmission) acquire(ctx context.Context, userID int64) (chatLease, int, error) {
	if g.client == nil || userID <= 0 {
		return chatLease{}, 0, errors.New("chat admission unavailable")
	}
	token, err := randomTicket()
	if err != nil {
		return chatLease{}, 0, err
	}
	lease := chatLease{strconv.FormatInt(userID, 10), token}
	retry, err := g.client.Eval(ctx, chatAcquireLua, chatKeys(lease.user), token).Int()
	if err != nil {
		return chatLease{}, 0, err
	}
	return lease, retry, nil
}

func (g redisChatAdmission) release(ctx context.Context, lease chatLease) error {
	return g.client.Eval(ctx, chatReleaseLua, chatKeys(lease.user)[:2], lease.token).Err()
}

func (s *Server) admitChat(ctx context.Context, userID int64) (func(), int, error) {
	gate := s.chatGate
	if gate == nil {
		gate = redisChatAdmission{s.redis}
	}
	lease, retry, err := gate.acquire(ctx, userID)
	if err != nil || retry > 0 {
		return nil, retry, err
	}
	return func() {
		// A disconnected request must still release its lease. Failure leaves a
		// bounded 50-second lease, never an unlimited admission bypass.
		releaseCtx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		if err := gate.release(releaseCtx, lease); err != nil && s.log != nil {
			s.log.Warn("chat_admission_release_failed")
		}
	}, 0, nil
}
