import json
from uuid import uuid4
from redis.exceptions import RedisError


class SessionError(Exception):
    def __init__(self, status, reason):
        self.status, self.reason = status, reason


ACQUIRE = """
local owner = redis.call('GET', KEYS[1])
if owner and owner ~= ARGV[1] then return -1 end
if not owner then redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[3]) end
if not redis.call('SET', KEYS[2], ARGV[2], 'NX', 'EX', 50) then return 0 end
redis.call('EXPIRE', KEYS[1], ARGV[3])
return 1
"""
RELEASE = "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) else return 0 end"


class Sessions:
    def __init__(self, redis, ttl=86400):
        self.redis, self.ttl = redis, ttl

    def keys(self, sid):
        return [f"stage7:session:{sid}:{suffix}" for suffix in ("owner", "lock", "history")]

    async def acquire(self, sid, user_id, request_id):
        sid = str(sid or uuid4())
        owner, lock, history = self.keys(sid)
        try:
            # Owner assignment and lock acquisition are atomic.
            result = await self.redis.eval(ACQUIRE, 2, owner, lock, str(user_id), request_id, self.ttl)
            if result == -1:
                raise SessionError(403, "session_forbidden")
            if result == 0:
                raise SessionError(409, "session_busy")
            try:
                raw = await self.redis.get(history)
                messages = json.loads(raw) if raw else []
                if not isinstance(messages, list):
                    raise ValueError()
                return sid, messages
            except BaseException:
                await self.redis.eval(RELEASE, 1, lock, request_id)
                raise
        except (RedisError, ValueError):
            raise SessionError(503, "session_unavailable") from None

    async def save(self, sid, history):
        owner, _, key = self.keys(sid)
        try:
            async with self.redis.pipeline(transaction=True) as pipe:
                pipe.set(key, json.dumps(history[-20:], ensure_ascii=False), ex=self.ttl)
                pipe.expire(owner, self.ttl)
                await pipe.execute()
        except RedisError:
            raise SessionError(503, "session_unavailable") from None

    async def release(self, sid, request_id):
        try:
            await self.redis.eval(RELEASE, 1, self.keys(sid)[1], request_id)
        except RedisError:
            raise SessionError(503, "session_unavailable") from None
