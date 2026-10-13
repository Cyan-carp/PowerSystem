import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import asyncpg
import httpx
import pytest
import pytest_asyncio

from ops_agent.reviews import ControlFailure, ReviewController
from review_contract import digest
from security_agent.main import create_app
from test_contract_service import config, FixtureProvider

import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from samples import CASES, install_base, seed


@pytest_asyncio.fixture
async def database():
    path=os.getenv('SEC_TEST_CONFIG_FILE')
    if not path:pytest.skip('SEC isolated PostgreSQL regression config required')
    cfg=json.loads(Path(path).read_text(encoding='utf-8'))
    port_allowed = cfg['port']==15432 or (cfg['port']==15433 and cfg.get('purpose')=='SEC-M2 isolated regression')
    if cfg['regression_database']!='sec_m1_regression_test' or cfg['host']!='127.0.0.1' or not port_allowed:
        raise RuntimeError('isolated_sec_database_required')
    pool=await asyncpg.create_pool(host=cfg['host'],port=cfg['port'],user=cfg['user'],
        password=Path(cfg['password_file']).read_text().strip(),database=cfg['regression_database'],min_size=1,max_size=6)
    await install_base(pool)
    cfg=config()
    class Provider(FixtureProvider):
        async def review(self,projected,timeout):
            result,usage=await super().review(projected,timeout)
            result['evidence_refs']=['synthetic-e1']
            return result,usage
    provider=Provider();app=create_app(cfg,provider)
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8100') as client:
        controller=ReviewController(pool,client,cfg.service_token,cfg.request_key,cfg.result_key)
        await controller.install()
        await pool.execute('TRUNCATE ma_security_review_events,ma_security_reviews,ma_execution_attempts,ma_approvals,ma_policy_versions,ma_tool_calls,ma_task_steps,ma_task_events,ma_outbox,ma_tasks,ma_session_guards,ma_asset_versions,ma_assets')
        try:yield pool,controller,provider,app
        finally:
            await pool.execute('DROP TRIGGER IF EXISTS sec_fixture_audit_failure ON ma_security_review_events')
            await pool.execute('DROP FUNCTION IF EXISTS sec_fixture_audit_failure()')
            await pool.close()


@pytest.mark.parametrize('value',['allow','deny','needs_clarification'])
async def test_SEC_C01_C03_persistence_bindings_and_idempotency(database,value):
    pool,controller,provider,app=database;provider.value=value
    params=await seed(pool,CASES[0]);req=await controller.build(**params)
    row=await controller.submit(**params)
    assert row['state']=='completed' and row['decision']['decision']==value and row['signature_validated']
    assert row['request_hash']==digest(req.model_dump(mode='json')) and row['expires_at']<=req.deadline
    assert await controller.valid(params['review_id'],params['user_id'],req)
    assert (await controller.submit(**params))['decision']==row['decision'] and provider.calls==1
    assert await pool.fetchval('SELECT count(*) FROM ma_security_review_events WHERE review_id=$1',params['review_id'])==2
    serialized=json.dumps({k:str(v) for k,v in row.items()})
    assert all(secret not in serialized for secret in ('S'*40,'Q'*40,'R'*40,'K'*40))


async def test_SEC_C03_cross_owner_and_conflict_zero_extra_model(database):
    pool,controller,provider,app=database;params=await seed(pool,CASES[0]);await controller.submit(**params)
    with pytest.raises(ControlFailure):await controller.get(params['review_id'],999)
    with pytest.raises(ControlFailure):await controller.submit(**{**params,'user_id':999})
    with pytest.raises(ControlFailure):await controller.submit(**{**params,'kind':'inspection_report'})
    assert provider.calls==1


@pytest.mark.parametrize('change', ['trace','args','epoch','step','owner'])
async def test_SEC_C03_saved_context_changed_no_effective_decision(database,change):
    pool,controller,provider,app=database;params=await seed(pool,CASES[0]);req=await controller.build(**params)
    await controller.submit(**params)
    if change=='trace':await pool.execute('UPDATE ma_tasks SET trace_id=$2 WHERE task_id=$1',params['task_id'],uuid4())
    if change=='epoch':await pool.execute('UPDATE ma_tasks SET execution_epoch=2 WHERE task_id=$1',params['task_id'])
    if change=='args':await pool.execute("UPDATE ma_task_steps SET args='{}' WHERE task_id=$1",params['task_id'])
    if change=='step':await pool.execute("UPDATE ma_task_steps SET tool='linux.get_memory_usage' WHERE task_id=$1",params['task_id'])
    if change=='owner':await pool.execute('UPDATE ma_tasks SET user_id=999 WHERE task_id=$1',params['task_id'])
    assert not await controller.valid(params['review_id'],params['user_id'],req)


async def test_SEC_C03_result_signature_and_payload_tampering(database):
    pool,controller,provider,app=database;params=await seed(pool,CASES[0])
    class Tamper:
        async def post(self,*args,**kwargs):
            response=await original.post(*args,**kwargs)
            data=response.json();data['reason']='篡改后的解释'
            return httpx.Response(200,json=data,headers=response.headers)
    original=controller.client;controller.client=Tamper()
    row=await controller.submit(**params)
    assert row['state']=='invalid' and row['decision'] is None and not row['signature_validated']


async def test_SEC_C05_secret_from_saved_material_zero_external_calls(database):
    pool,controller,provider,app=database;params=await seed(pool,CASES[0])
    envelope=json.loads(await pool.fetchval('SELECT envelope FROM ma_tasks WHERE task_id=$1',params['task_id']))
    envelope['intent']='SEC-M1 synthetic:Bearer synthetic-secret'
    await pool.execute('UPDATE ma_tasks SET envelope=$2::jsonb WHERE task_id=$1',params['task_id'],json.dumps(envelope))
    with pytest.raises(ValueError):await controller.submit(**params)
    assert provider.calls==0 and await pool.fetchval('SELECT count(*) FROM ma_security_reviews')==0


async def test_SEC_C06_audit_transaction_failure_never_returns_allow(database):
    pool,controller,provider,app=database;params=await seed(pool,CASES[0])
    await pool.execute("CREATE FUNCTION sec_fixture_audit_failure() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.state='completed' THEN RAISE EXCEPTION 'synthetic audit unavailable'; END IF; RETURN NEW; END $$")
    await pool.execute('CREATE TRIGGER sec_fixture_audit_failure BEFORE INSERT ON ma_security_review_events FOR EACH ROW EXECUTE FUNCTION sec_fixture_audit_failure()')
    with pytest.raises(asyncpg.PostgresError):await controller.submit(**params)
    row=await controller.get(params['review_id'],params['user_id'])
    assert row['state']=='pending' and row['decision'] is None and not row['signature_validated']
    await pool.execute('DROP TRIGGER sec_fixture_audit_failure ON ma_security_review_events')
    assert await controller.recover()==1
    assert (await controller.submit(**params))['state']=='unavailable' and provider.calls==1


async def test_SEC_C06_startup_recovery_is_not_replayed(database):
    pool,controller,provider,app=database;params=await seed(pool,CASES[0]);request=await controller.build(**params)
    await controller.reserve(request);assert await controller.recover()==1
    row=await controller.submit(**params)
    assert row['state']=='unavailable' and row['error_code']=='review_interrupted' and provider.calls==0


async def test_duplicate_concurrent_review_single_call(database):
    pool,controller,provider,app=database;params=await seed(pool,CASES[0])
    rows=await asyncio.gather(controller.submit(**params),controller.submit(**params))
    assert provider.calls==1 and any(r['state']=='completed' for r in rows)
    assert await pool.fetchval('SELECT count(*) FROM ma_security_reviews')==1


async def test_context_changed_during_review_rejected(database):
    pool,controller,provider,app=database;params=await seed(pool,CASES[0]);original=provider.review
    async def changed(*args):
        value=await original(*args)
        await pool.execute('UPDATE ma_tasks SET execution_epoch=2 WHERE task_id=$1',params['task_id'])
        return value
    provider.review=changed
    row=await controller.submit(**params)
    assert row['state']=='invalid' and row['error_code']=='security_review_stale' and row['decision'] is None


async def test_expired_and_cancelled_reviews_do_not_authorize(database):
    pool,controller,provider,app=database;params=await seed(pool,CASES[0]);req=await controller.build(**params)
    await controller.submit(**params)
    await pool.execute("UPDATE ma_security_reviews SET expires_at=now()-interval '1 second' WHERE review_id=$1",params['review_id'])
    assert not await controller.valid(params['review_id'],params['user_id'],req)
    params=await seed(pool,CASES[0]);await pool.execute("UPDATE ma_tasks SET status='cancelled' WHERE task_id=$1",params['task_id'])
    with pytest.raises(ControlFailure):await controller.submit(**params)
    assert provider.calls==1


@pytest.mark.parametrize('failure,code',[(httpx.ReadTimeout,'security_timeout'),
                                      (httpx.ConnectError,'security_transport_unavailable')])
async def test_controller_transport_faults_are_distinct_and_durable(database,failure,code):
    pool,controller,provider,app=database;params=await seed(pool,CASES[0])
    class FailingClient:
        async def post(self,*args,**kwargs):raise failure('synthetic transport fault')
    controller.client=FailingClient()
    row=await controller.submit(**params)
    assert row['state']=='unavailable' and row['error_code']==code and row['duration_ms']>=0
    assert row['decision'] is None and not row['signature_validated'] and provider.calls==0
    assert await pool.fetchval('SELECT reason FROM ma_security_review_events WHERE review_id=$1 ORDER BY event_seq DESC LIMIT 1',params['review_id'])==code


async def test_timeout_exception_still_obeys_parent_remaining_budget(database):
    pool,original,provider,app=database;params=await seed(pool,CASES[0])
    await pool.execute("UPDATE ma_tasks SET deadline=now()+interval '5 seconds' WHERE task_id=$1",params['task_id'])
    timeouts=[]
    class CapturingClient:
        async def post(self,*args,**kwargs):
            timeouts.append(kwargs['timeout']);return await original.client.post(*args,**kwargs)
    controller=ReviewController(pool,CapturingClient(),original.service_token,original.request_key,original.result_key,
        model='gpt-5.6-sol',timeout=120,acceptance_timeout_exception=True)
    row=await controller.submit(**params)
    assert row['state']=='completed' and len(timeouts)==1 and 0<timeouts[0]<=5


@pytest.mark.parametrize('failure,code',[('extra','decision_schema_invalid'),('reference','invalid_evidence_ref')])
async def test_invalid_model_category_is_audited_without_raw_material(database,failure,code):
    pool,controller,provider,app=database;params=await seed(pool,CASES[0]);original=provider.review
    async def wrong(*args):
        result,usage=await original(*args)
        if failure=='extra':result['extra']='synthetic forbidden field'
        else:result['evidence_refs']=['invented-ref']
        return result,usage
    provider.review=wrong
    row=await controller.submit(**params)
    assert row['state']=='invalid' and row['validation_code']==code and row['decision'] is None
    assert not row['signature_validated'] and row['duration_ms']>=0
    reason=await pool.fetchval('SELECT reason FROM ma_security_review_events WHERE review_id=$1 ORDER BY event_seq DESC LIMIT 1',params['review_id'])
    assert reason=='model_invalid_decision:'+code and 'synthetic forbidden field' not in reason
