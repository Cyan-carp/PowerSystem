import json
from pathlib import Path
import asyncpg


class Store:
    def __init__(self, pool):
        self.pool = pool

    @classmethod
    async def open(cls, url):
        pool = await asyncpg.create_pool(url, min_size=1, max_size=4, command_timeout=10)
        async with pool.acquire() as conn:
            async with conn.transaction():
                for path in sorted((Path(__file__).resolve().parents[1] / "deploy").glob("[0-9][0-9][0-9]_*.sql")):
                    await conn.execute(path.read_text(encoding="utf-8"))
        return cls(pool)

    async def ensure(self, sid, user, title, create=False):
        if create:
            await self.pool.execute("INSERT INTO rag_sessions(id,user_id,title) VALUES($1,$2,$3)", sid, user, title[:100])
        row = await self.pool.fetchrow("SELECT user_id FROM rag_sessions WHERE id=$1", sid)
        return bool(row and row["user_id"] == user)

    async def turns(self, sid):
        rows = await self.pool.fetch("SELECT id,kind,parent_turn_id,question,response,model_messages,created_at FROM rag_turns WHERE session_id=$1 ORDER BY id", sid)
        return [{"id": r["id"], "question": r["question"], "response": json.loads(r["response"]),
                 "kind": r["kind"], "parent_turn_id": r["parent_turn_id"],
                 "model_messages": json.loads(r["model_messages"]), "created_at": r["created_at"].isoformat()} for r in rows]

    async def verification(self, sid, vid):
        row = await self.pool.fetchrow("SELECT id,response FROM rag_turns WHERE session_id=$1 AND verification_id=$2", sid, vid)
        return {"id": row["id"], "response": json.loads(row["response"])} if row else None

    async def save(self, sid, question, response, messages, kind="chat", parent_turn_id=None, verification_id=None):
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                turn_id = await conn.fetchval("INSERT INTO rag_turns(session_id,question,response,model_messages,kind,parent_turn_id,verification_id) VALUES($1,$2,$3::jsonb,$4::jsonb,$5,$6,$7) RETURNING id",
                    sid, question, json.dumps(response, ensure_ascii=False), json.dumps(messages, ensure_ascii=False), kind, parent_turn_id, verification_id)
                await conn.execute("UPDATE rag_sessions SET updated_at=now() WHERE id=$1", sid)
        return turn_id

    async def list(self, user, offset=0):
        rows = await self.pool.fetch("SELECT id,title,created_at,updated_at FROM rag_sessions WHERE user_id=$1 ORDER BY updated_at DESC LIMIT 50 OFFSET $2", user, offset)
        return [{"id": str(r["id"]), "title": r["title"], "created_at": r["created_at"].isoformat(), "updated_at": r["updated_at"].isoformat()} for r in rows]

    async def delete(self, sid, user):
        return await self.pool.execute("DELETE FROM rag_sessions WHERE id=$1 AND user_id=$2", sid, user) == "DELETE 1"
