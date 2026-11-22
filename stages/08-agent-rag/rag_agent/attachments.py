"""Owner-scoped local attachment search, separate from every model and tool path."""
import asyncio
import json
import os
import re
import sys
from uuid import UUID, uuid4

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .attachment_parser import AttachmentError, file_suffix


MAX_PER_QUERY = 3
MAX_PER_SESSION = 20
MAX_DRAFTS = 12
MAX_RESULTS = 6
TERM = re.compile(r'[\u3400-\u9fff]+|[a-z0-9]+(?:[-_.:/][a-z0-9]+)*', re.I)
FOLLOW_UP = re.compile(r'继续|刚才|上面|上一|这个|那个|这条|那条|它们?|详细一点|再详细|follow up|that (part|step)', re.I)


class Query(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID
    session_id: UUID | None = None
    attachment_ids: list[UUID] = Field(min_length=1, max_length=MAX_PER_QUERY)
    question: str = Field(min_length=1, max_length=4000)


def terms(value):
    words = set()
    for word in TERM.findall(value.casefold()):
        if '\u3400' <= word[0] <= '\u9fff':
            words.update(word[i:i + 2] for i in range(len(word) - 1))
            if len(word) == 1:
                words.add(word)
        else:
            words.add(word)
    return words


def search(question, files, previous=''):
    wanted = terms(question)
    context = terms(previous) if FOLLOW_UP.search(question) else set()
    if not wanted:
        return []
    candidates = []
    for file in files:
        for part in json.loads(file['excerpts']):
            available = terms(part['text'])
            hit = len(wanted & available)
            prior = len(context & available)
            if hit or prior:
                candidates.append((3 * hit + prior, {
                    'attachment_id': str(file['id']), 'name': file['name'],
                    'location': part['location'], 'text': part['text'],
                }))
    candidates.sort(key=lambda item: -item[0])
    return [item for _, item in candidates[:MAX_RESULTS]]


def file_view(row):
    return {'id': str(row['id']), 'name': row['name'], 'size_bytes': row['size_bytes']}


def turn_view(row):
    return {'id': str(row['id']), 'question': row['question'], 'created_at': row['created_at'].isoformat(),
            'attachment_result': {'attachment_ids': [str(value) for value in row['attachment_ids']],
                                  'excerpts': json.loads(row['excerpts'])}}


async def owner_lock(conn, user_id):
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('rag-attachments:' || $1::text, 0))", str(user_id))


class Attachments:
    def __init__(self, pool):
        self.pool = pool
        self.parsers = asyncio.Semaphore(2)

    async def extract(self, name, data):
        try:
            suffix = file_suffix(name)
        except AttachmentError as error:
            raise HTTPException(400, str(error)) from None
        if self.parsers.locked():
            raise HTTPException(429, '附件解析繁忙，请稍后重试')
        async with self.parsers:
            env = {key: value for key, value in os.environ.items() if key in ('SYSTEMROOT', 'WINDIR', 'PYTHONPATH')}
            process = await asyncio.create_subprocess_exec(
                sys.executable, '-B', '-m', 'rag_agent.attachment_parser', suffix, env=env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                output, _ = await asyncio.wait_for(process.communicate(data), timeout=10)
            except (TimeoutError, asyncio.CancelledError):
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                await process.wait()
                if asyncio.current_task().cancelling():
                    raise
                raise HTTPException(422, '附件解析超过处理时限，请拆分文件') from None
        if process.returncode != 0 or len(output) > 4 * 1024 * 1024:
            raise HTTPException(422, '附件超出解析资源限制')
        result = json.loads(output)
        if 'error' in result:
            raise HTTPException(422, result['error'])
        return result['excerpts']

    async def save(self, user_id, name, size, excerpts):
        async with self.pool.acquire() as conn, conn.transaction():
            await owner_lock(conn, user_id)
            count = await conn.fetchval('''SELECT count(*) FROM rag_attachments WHERE user_id=$1
                AND session_id IS NULL AND created_at > now() - interval '24 hours' ''', user_id)
            if count >= MAX_DRAFTS:
                raise HTTPException(429, '未发送附件过多，请先移除部分附件')
            row = await conn.fetchrow('''INSERT INTO rag_attachments(id,user_id,name,size_bytes,excerpts)
                VALUES($1,$2,$3,$4,$5::jsonb) RETURNING id,name,size_bytes''',
                uuid4(), user_id, name, size, json.dumps(excerpts, ensure_ascii=False))
        return file_view(row)

    async def discard(self, user_id, attachment_id):
        await self.pool.execute('DELETE FROM rag_attachments WHERE user_id=$1 AND id=$2 AND session_id IS NULL',
                                user_id, attachment_id)

    async def expire(self):
        while True:
            try:
                await self.pool.execute("DELETE FROM rag_attachments WHERE session_id IS NULL AND created_at <= now() - interval '24 hours'")
            except Exception:
                # The query path also rejects expired drafts when cleanup is delayed.
                pass
            await asyncio.sleep(900)

    async def history(self, user_id, session_id):
        async with self.pool.acquire() as conn, conn.transaction(isolation='repeatable_read', readonly=True):
            live = await conn.fetchval('SELECT id FROM rag_sessions WHERE id=$1 AND user_id=$2 AND deleted_at IS NULL', session_id, user_id)
            if not live:
                return [], []
            files = await conn.fetch('SELECT id,name,size_bytes FROM rag_attachments WHERE session_id=$1 AND user_id=$2 ORDER BY created_at,id',
                                     session_id, user_id)
            turns = await conn.fetch('SELECT id,question,attachment_ids,excerpts,created_at FROM rag_attachment_turns WHERE session_id=$1 AND user_id=$2 ORDER BY id',
                                     session_id, user_id)
        return [file_view(row) for row in files], [turn_view(row) for row in turns]

    async def query(self, user_id, body):
        question = body.question.strip()
        ids = body.attachment_ids
        if not question or '\x00' in question or len(set(ids)) != len(ids):
            raise HTTPException(400, '请填写问题并选择有效附件')
        async with self.pool.acquire() as conn, conn.transaction():
            await owner_lock(conn, user_id)
            existing = await conn.fetchrow('SELECT * FROM rag_attachment_turns WHERE user_id=$1 AND request_id=$2', user_id, body.request_id)
            if existing:
                if (existing['question'] != question or existing['attachment_ids'] != ids or
                        existing['request_session_id'] != body.session_id):
                    raise HTTPException(409, '提交编号已用于另一条附件问题')
                live = await conn.fetchval('SELECT id FROM rag_sessions WHERE id=$1 AND user_id=$2 AND deleted_at IS NULL FOR UPDATE',
                                           existing['session_id'], user_id)
                if not live:
                    raise HTTPException(404, '会话不存在')
                return {'session_id': str(live), 'turn': turn_view(existing)}

            sid = body.session_id or uuid4()
            if body.session_id:
                live = await conn.fetchval('SELECT id FROM rag_sessions WHERE id=$1 AND user_id=$2 AND deleted_at IS NULL FOR UPDATE', sid, user_id)
                if not live:
                    raise HTTPException(404, '会话不存在')
            else:
                await conn.execute('INSERT INTO rag_sessions(id,user_id,title) VALUES($1,$2,$3)', sid, user_id, '附件问答')

            files = await conn.fetch('''SELECT id,session_id,name,excerpts FROM rag_attachments WHERE user_id=$1 AND id=ANY($2::uuid[])
                AND (session_id=$3 OR (session_id IS NULL AND created_at > now() - interval '24 hours')) FOR UPDATE''', user_id, ids, sid)
            if len(files) != len(ids):
                raise HTTPException(404, '附件已失效或不属于当前会话')
            attached = await conn.fetchval('SELECT count(*) FROM rag_attachments WHERE session_id=$1 AND user_id=$2', sid, user_id)
            if attached + sum(row['session_id'] is None for row in files) > MAX_PER_SESSION:
                raise HTTPException(400, '会话附件已满，请新建会话')
            files_by_id = {row['id']: row for row in files}
            ordered = [files_by_id[file_id] for file_id in ids]
            previous = await conn.fetchval('''SELECT question FROM rag_attachment_turns WHERE session_id=$1
                AND user_id=$2 AND attachment_ids && $3::uuid[] ORDER BY id DESC LIMIT 1''', sid, user_id, ids)
            excerpts = await asyncio.to_thread(search, question, ordered, previous or '')
            await conn.execute('UPDATE rag_attachments SET session_id=$1 WHERE user_id=$2 AND id=ANY($3::uuid[]) AND session_id IS NULL', sid, user_id, ids)
            row = await conn.fetchrow('''INSERT INTO rag_attachment_turns(session_id,user_id,request_id,request_session_id,attachment_ids,question,excerpts)
                VALUES($1,$2,$3,$4,$5,$6,$7::jsonb) RETURNING id,question,attachment_ids,excerpts,created_at''',
                sid, user_id, body.request_id, body.session_id, ids, question, json.dumps(excerpts, ensure_ascii=False))
            await conn.execute('UPDATE rag_sessions SET updated_at=now() WHERE id=$1', sid)
        return {'session_id': str(sid), 'turn': turn_view(row)}


def routes(app):
    def service(user_id):
        if user_id <= 0:
            raise HTTPException(400, '账号不可用')
        attachments = getattr(app.state, 'attachments', None)
        if attachments is None:
            raise HTTPException(503, '助手附件暂不可用')
        return attachments

    @app.post('/internal/agent/attachments')
    async def upload(request: Request, user_id: int, name: str):
        attachments = service(user_id)
        data = await request.body()
        excerpts = await attachments.extract(name, data)
        if await request.is_disconnected():
            raise HTTPException(409, '附件上传已取消')
        return await attachments.save(user_id, name, len(data), excerpts)

    @app.delete('/internal/agent/attachments/{attachment_id}')
    async def discard(attachment_id: UUID, user_id: int):
        await service(user_id).discard(user_id, attachment_id)
        return {'removed': True}

    @app.post('/internal/agent/attachment-queries')
    async def query(body: Query, user_id: int):
        return await service(user_id).query(user_id, body)
