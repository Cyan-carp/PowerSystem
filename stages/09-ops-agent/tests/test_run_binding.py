"""S03 child binding through the real stage9 migration/store, without execution."""
import os
from datetime import datetime,timezone
from uuid import uuid4
import pytest
from fastapi import HTTPException
from ops_agent.config import Config
from ops_agent.schemas import Task
from ops_agent.store import Store


@pytest.mark.asyncio
async def test_run_binding_created_atomically_and_idempotency_includes_turn():
    url=os.environ.get('RAG_RUN_TEST_URL','')
    if not url:pytest.skip('independent S03 PostgreSQL acceptance database required')
    assert url.startswith('postgresql://s03_test@127.0.0.1:25433/') and url.endswith('_test')
    cfg=Config(database_url=url,redis_url='redis://127.0.0.1:1',service_token='x'*32,actor_key='a'*32,envelope_key='b'*32,enabled=True,user_active_limit=10)
    store=await Store.open(cfg)
    user=2100000000+int(uuid4().hex[:6],16)
    values=dict(protocol='ma-run/v1',task_id=uuid4(),trace_id=uuid4(),idempotency_key=uuid4(),session_id=uuid4(),run_id=uuid4(),parent_turn_id=1234,actor=dict(user_id=user,role='operator'),intent='只读隔离测试',user_request='查看设备状态',created_at=datetime.now(timezone.utc))
    task=Task(**values)
    tid,created=await store.create(task)
    assert created
    row=await store.get(tid,user)
    assert row['parent_turn_id']==1234 and row['run_id']==values['run_id'] and not row['link_pending']
    report=await store.report(tid,user)
    assert report['protocol']=='ma-run/v1' and report['run_id']==str(values['run_id']) and report['parent_turn_id']==1234
    assert (await store.create(task))==(tid,False)
    with pytest.raises(HTTPException) as error:await store.create(Task(**{**values,'parent_turn_id':1235}))
    assert error.value.status_code==409
    reopened=await Store.open(cfg)
    assert (await reopened.get(tid,user))['run_id']==values['run_id']
    await reopened.pool.close();await store.pool.close()


def test_run_requires_original_parent_turn():
    values=dict(protocol='ma-run/v1',task_id=uuid4(),trace_id=uuid4(),idempotency_key=uuid4(),session_id=uuid4(),run_id=uuid4(),actor=dict(user_id=1,role='operator'),intent='只读',user_request='只读',created_at=datetime.now(timezone.utc))
    with pytest.raises(ValueError):Task(**values)
    with pytest.raises(ValueError):Task(**{**values,'parent_turn_id':1234,'actor':dict(user_id=1,role='admin'),'operation':'restart_drill_container','constraints':dict(max_risk_class='R2')})
