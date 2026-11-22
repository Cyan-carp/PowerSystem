import asyncio
import os
from uuid import uuid4
import httpx
import pytest
import pytest_asyncio
from rag_agent.config import Config
from rag_agent.main import create_app
from rag_agent.store import Store
from rag_agent.tools import Tools


class Redis:
    def __init__(self): self.locks = {}
    async def set(self, key, value, nx=False, ex=0):
        if nx and key in self.locks: return False
        self.locks[key] = value; return True
    async def eval(self, code, count, key, value):
        if self.locks.get(key) == value: self.locks.pop(key, None)


class Provider:
    def __init__(self, delay=0): self.delay = delay; self.messages = []
    async def complete(self, messages, schemas=None):
        self.messages = messages
        await asyncio.sleep(self.delay)
        return {"role":"assistant", "content":"这是普通技术问题的回答。"}


@pytest_asyncio.fixture
async def environment():
    if not os.getenv("RAG_DATABASE_URL"): pytest.skip("independent test database not configured")
    cfg = Config.load()
    store = await Store.open(cfg.database_url)
    user = 900000 + int(uuid4().hex[:4], 16)
    yield cfg, store, user
    sessions = await store.list(user)
    for row in sessions: await store.delete(__import__('uuid').UUID(row['id']), user)
    await store.pool.close()


def headers(cfg): return {"X-PowerSystem-Token":cfg.service_token, "Authorization":"Bearer test-user-token"}


@pytest.mark.asyncio
async def test_history_survives_new_app_and_user_isolation(environment):
    cfg, store, user = environment
    cfg.audit_dir = cfg.audit_dir / "tests"
    app = create_app(cfg, store, Redis(), Provider(), Tools(cfg, None, None))
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        result = await client.post("/internal/agent/chat",headers=headers(cfg),json={"user_id":user,"message":"解释TCP"})
        assert result.status_code == 200 and result.json()["status"] == "answered"
        sid = result.json()["session_id"]
        denied = await client.get(f"/internal/agent/sessions/{sid}?user_id={user+1}",headers=headers(cfg))
        assert denied.status_code == 404
        denied = await client.post("/internal/agent/chat",headers=headers(cfg),json={"user_id":user+1,"message":"它","session_id":sid})
        assert denied.status_code == 404
    provider = Provider()
    restarted = create_app(cfg, store, Redis(), provider, Tools(cfg, None, None))
    async with restarted.router.lifespan_context(restarted), httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted), base_url="http://test") as client:
        history = await client.get(f"/internal/agent/sessions/{sid}?user_id={user}",headers=headers(cfg))
        assert history.json()["turns"][0]["question"] == "解释TCP"
        follow = await client.post("/internal/agent/chat",headers=headers(cfg),json={"user_id":user,"message":"继续解释","session_id":sid})
        assert follow.json()["status"] == "answered" and "解释TCP" in str(provider.messages)
        forged = await client.delete(f"/internal/agent/sessions/{sid}?user_id={user+1}",headers=headers(cfg))
        assert forged.status_code == 404
        deleted = await client.delete(f"/internal/agent/sessions/{sid}?user_id={user}",headers=headers(cfg))
        assert deleted.json()["deleted"] is True
        assert (await client.get(f"/internal/agent/sessions/{sid}?user_id={user}",headers=headers(cfg))).status_code == 404


@pytest.mark.asyncio
async def test_token_input_and_busy_session(environment):
    cfg, store, user = environment
    cfg.audit_dir = cfg.audit_dir / "tests"
    sid = uuid4(); await store.ensure(sid, user, "并发测试", create=True)
    redis = Redis(); app = create_app(cfg,store,redis,Provider(.2),Tools(cfg,None,None))
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        assert (await client.get('/internal/agent/sessions',params={"user_id":user})).status_code == 401
        bad = await client.post('/internal/agent/chat',headers=headers(cfg),json={"user_id":user,"message":"","unexpected":"private"})
        assert bad.status_code == 422 and "private" not in bad.text
        bad = await client.post('/internal/agent/chat',headers=headers(cfg),json={"user_id":user,"message":" "})
        assert bad.status_code == 400
        task = asyncio.create_task(client.post('/internal/agent/chat',headers=headers(cfg),json={"user_id":user,"message":"长回答","session_id":str(sid)}))
        await asyncio.sleep(.05)
        busy = await client.delete(f'/internal/agent/sessions/{sid}?user_id={user}',headers=headers(cfg))
        assert busy.status_code == 409
        assert (await task).status_code == 200 and not redis.locks


@pytest.mark.asyncio
async def test_session_archive_owner_persistence_restore_and_delete(environment):
    cfg, store, user = environment
    sid = uuid4()
    assert await store.ensure(sid, user, "archive lifecycle", create=True)
    await store.save(sid, "question", {"answer_markdown": "retained"}, [])
    app = create_app(cfg, store, Redis(), Provider(), Tools(cfg, None, None), run_worker=False)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        path = f"/internal/agent/sessions/{sid}"
        # Authentication and ownership apply to both lifecycle actions.
        assert (await client.post(path + "/archive", params={"user_id": user})).status_code == 401
        for action in ("archive", "restore"):
            assert (await client.post(path + "/" + action, params={"user_id": user + 1}, headers=headers(cfg))).status_code == 404
        for _ in range(2):
            result = await client.post(path + "/archive", params={"user_id": user}, headers=headers(cfg))
            assert result.status_code == 200 and result.json() == {"archived": True}
        active = await client.get("/internal/agent/sessions", params={"user_id": user}, headers=headers(cfg))
        assert not any(row["id"] == str(sid) for row in active.json()["list"])
        archived = await client.get("/internal/agent/sessions", params={"user_id": user, "archived": "true"}, headers=headers(cfg))
        assert any(row["id"] == str(sid) and row["archived"] for row in archived.json()["list"])
        history = await client.get(path, params={"user_id": user}, headers=headers(cfg))
        assert history.json()["turns"][0]["question"] == "question"
        # Reopening the pool reapplies migrations and must preserve the saved flag.
        reopened = await Store.open(cfg.database_url)
        try:
            assert any(row["id"] == str(sid) for row in await reopened.list(user, archived=True))
        finally:
            await reopened.pool.close()
        restored = await client.post(path + "/restore", params={"user_id": user}, headers=headers(cfg))
        assert restored.json() == {"archived": False}
        assert any(row["id"] == str(sid) for row in await store.list(user, archived=False))
        await client.post(path + "/archive", params={"user_id": user}, headers=headers(cfg))
        assert (await client.delete(path, params={"user_id": user}, headers=headers(cfg))).status_code == 200
        assert (await client.post(path + "/restore", params={"user_id": user}, headers=headers(cfg))).status_code == 404
