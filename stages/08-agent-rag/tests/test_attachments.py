import asyncio
import io
import os
import zipfile
from uuid import UUID, uuid4

import asyncpg
import httpx
import pytest

from rag_agent.attachment_parser import AttachmentError, parse
from rag_agent.config import Config
from rag_agent.main import create_app
from rag_agent.store import Store
from rag_agent.tools import Tools


def zip_bytes(parts):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as archive:
        for name, value in parts.items():
            archive.writestr(name, value)
    return out.getvalue()


def pdf_bytes(text=None):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    if text:
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({
            NameObject('/Helvetica'): DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})})})
        stream = DecodedStreamObject()
        stream.set_data(f'BT /Helvetica 12 Tf 10 100 Td ({text}) Tj ET'.encode('ascii'))
        page.replace_contents(stream)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def test_attachment_formats_and_missing_text():
    docx = zip_bytes({'word/document.xml': '<document><body><p><r><t>逆变器检查</t></r></p></body></document>'})
    xlsx = zip_bytes({
        'xl/workbook.xml': '<workbook xmlns:r="urn:test"><sheets><sheet name="巡检" r:id="r1"/></sheets></workbook>',
        'xl/_rels/workbook.xml.rels': '<Relationships><Relationship Id="r1" Target="worksheets/sheet1.xml"/></Relationships>',
        'xl/worksheets/sheet1.xml': '<worksheet><sheetData><row r="11"><c r="B11" t="inlineStr"><is><t>逆变器检查</t></is></c></row></sheetData></worksheet>',
    })
    samples = [('.pdf', pdf_bytes('Inverter inspection'), 'Inverter inspection', '第 1 页'),
               ('.docx', docx, '逆变器检查', '第 1 段'), ('.xlsx', xlsx, '逆变器检查', '巡检 · 第 11 行'),
               ('.csv', '设备,现象\n逆变器,温度高'.encode(), '现象 温度高', '第 2 行'),
               ('.txt', b'TDengine timeout', 'TDengine timeout', '第 1–1 行'),
               ('.log', b'connection reset', 'connection reset', '第 1–1 行')]
    for suffix, payload, expected, location in samples:
        assert any(expected in row['text'] and row['location'] == location for row in parse(payload, suffix))
    with pytest.raises(AttachmentError, match='没有可检索'):
        parse(pdf_bytes(), '.pdf')
    with pytest.raises(AttachmentError, match='无法解析'):
        parse(b'damaged package', '.docx')


@pytest.mark.asyncio
async def test_attachment_history_ownership_and_model_isolation(tmp_path):
    dsn = os.getenv('BUSINESS_SUPPLEMENT_TEST_DSN')
    if not dsn:
        pytest.skip('requires isolated PostgreSQL fixture')
    from urllib.parse import urlsplit

    target = urlsplit(dsn)
    assert target.hostname == '127.0.0.1' and target.port == 29873 and target.path == '/postgres'
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=1, statement_cache_size=0, timeout=8, command_timeout=8)
    assert await pool.fetchval("SELECT EXISTS (SELECT 1 FROM supplement_fixture WHERE marker='isolated')")
    store = Store(pool)
    user = 200000 + int(uuid4().hex[:6], 16)
    cfg = Config('fixture', 'http://127.0.0.1:1', '', 'fixture-service-token' * 3, dsn, '', audit_dir=tmp_path)
    messages = []

    class Provider:
        async def complete(self, payload, schemas=None):
            messages.extend(payload)
            return {'role': 'assistant', 'content': '正常助手回答。'}

    class Redis:
        async def set(self, *args, **kwargs):
            return True

        async def eval(self, *args, **kwargs):
            return 1

    app = create_app(cfg, store, Redis(), Provider(), Tools(cfg, None, None), run_worker=False)
    headers = {'X-PowerSystem-Token': cfg.service_token, 'Authorization': 'Bearer fixture'}
    try:
        async with asyncio.timeout(30), app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as client:
            private = 'LOCAL-ONLY-INSPECTION-79C02'
            params = {'user_id': user, 'name': 'private-fixture.txt'}
            upload = '/internal/agent/attachments'
            assert (await client.post(upload, params=params, content=private.encode())).status_code == 401
            assert (await client.post(upload, params=params, headers=headers, content=b'x' * (5 * 1024 * 1024 + 1))).status_code == 413
            saved = await client.post(upload, params=params, headers=headers, content=f'逆变器检查 {private}'.encode())
            assert saved.status_code == 200, saved.text
            attachment = saved.json()['id']
            body = {'request_id': str(uuid4()), 'attachment_ids': [attachment], 'question': '逆变器检查'}
            query = '/internal/agent/attachment-queries'
            assert (await client.post(query, params={'user_id': user + 1}, json=body, headers=headers)).status_code == 404
            answer = await client.post(query, params={'user_id': user}, json=body, headers=headers)
            assert answer.status_code == 200, answer.text
            assert private in answer.text and '第 1' in answer.text and not messages
            assert (await client.post(query, params={'user_id': user}, json=body, headers=headers)).json() == answer.json()
            sid = answer.json()['session_id']
            more = {**body, 'session_id': sid, 'request_id': str(uuid4()), 'question': '继续说刚才这个'}
            follow = await client.post(query, params={'user_id': user}, json=more, headers=headers)
            assert follow.status_code == 200 and private in follow.text
            unrelated = await client.post(query, params={'user_id': user}, json={**more, 'request_id': str(uuid4()), 'question': '发电量'}, headers=headers)
            assert unrelated.status_code == 200 and unrelated.json()['turn']['attachment_result']['excerpts'] == []
            endpoint = f'/internal/agent/sessions/{sid}'
            assert (await client.post(endpoint + '/archive', params={'user_id': user}, headers=headers)).status_code == 200
            history = await client.get(endpoint, params={'user_id': user}, headers=headers)
            assert history.status_code == 200 and private in history.text
            ordinary = await client.post('/internal/agent/chat', headers=headers, json={'user_id': user, 'session_id': sid, 'message': '解释 TCP 握手'})
            assert ordinary.status_code == 200, ordinary.text
            assert messages and private not in str(messages) and '逆变器检查' not in str(messages)
            # Exercise the soft-delete path as well as ownership of the history API.
            assert (await client.get(endpoint, params={'user_id': user + 1}, headers=headers)).status_code == 404
            await pool.execute('UPDATE rag_sessions SET deleted_at=now() WHERE id=$1', UUID(sid))
            assert await pool.fetchval('SELECT count(*) FROM rag_attachments WHERE user_id=$1', user) == 0
            assert await pool.fetchval('SELECT count(*) FROM rag_attachment_turns WHERE user_id=$1', user) == 0
    finally:
        pool.terminate()
