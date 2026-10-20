"""Production model migration keeps historical scopes and all R0 gates distinct."""
import pytest
from pydantic import ValidationError
from ops_agent.runtime_profile import context, check_context
from ops_agent.runtime_reviews import SecurityGates
from review_contract import ReviewRequest, M4_SERVER_SCOPE, M4_SERVER_POLICY, projection
from test_m4 import cfg_local, request

NEW_MODEL = 'deepseek-v4.1-flash'
OLD_MODEL = 'deepseek-v4-flash-0731'

def server_cfg():
    cfg = cfg_local()
    cfg.runtime_profile = 'm4-server-readonly'
    cfg.database_url = 'postgresql://u:p@postgres:5432/ma_ops'
    cfg.backend_url = 'http://api:8080'
    cfg.supervisor_url = 'http://agent-rag:8093'
    cfg.executor_url = 'http://ops-executor:8095'
    cfg.security_url = 'http://security-agent:8100'
    cfg.model = NEW_MODEL
    return cfg

def test_new_production_model_is_pinned_and_r0_is_preserved():
    cfg = server_cfg(); cfg.validate()
    assert context(cfg)['policy_version'] == M4_SERVER_POLICY == 'sec-m4-policy/v6'
    assert context(cfg)['scope_version'] == M4_SERVER_SCOPE == 'sec-m4-project-readonly/v6'
    for model in (OLD_MODEL, 'other-model'):
        cfg.model = model
        with pytest.raises(ValueError, match='unlicensed_runtime_model'): cfg.validate()
    cfg.model = NEW_MODEL; cfg.write_enabled = True
    with pytest.raises(ValueError, match='server_read_only'): cfg.validate()

def test_old_configuration_and_permission_context_cannot_be_reused():
    cfg = server_cfg(); saved = context(cfg)
    saved['policy_version'] = 'sec-m4-policy/v5'
    saved['scope_version'] = 'sec-m4-project-readonly/v5'
    with pytest.raises(ValueError, match='security_review_stale'):
        check_context({'runtime_context':saved}, cfg)
    cfg.model = OLD_MODEL; old = context(cfg); cfg.model = NEW_MODEL
    with pytest.raises(ValueError, match='security_review_stale'):
        check_context({'runtime_context':old}, cfg)

def egress_request(policy, scope, model):
    raw = request(M4_SERVER_SCOPE)
    raw.update(kind='data_egress', policy_version=policy, scope_version=scope)
    raw['egress'] = {'destination':'existing-gateway', 'scope_version':scope,
        'receiver':'operation', 'model':model,
        'fields':['normalized_intent','tool','asset','evidence','policy_version'], 'payload_hash':'d'*64}
    return raw

@pytest.mark.parametrize('policy,scope,model', [
    ('sec-m4-policy/v5','sec-m4-project-readonly/v5',OLD_MODEL),
    (M4_SERVER_POLICY,M4_SERVER_SCOPE,NEW_MODEL),
])
def test_egress_model_is_bound_to_its_permission_version(policy, scope, model):
    raw = egress_request(policy,scope,model)
    result = projection(ReviewRequest.model_validate(raw))
    assert result['egress']['model'] == model
    raw['egress']['model'] = NEW_MODEL if model == OLD_MODEL else OLD_MODEL
    with pytest.raises(ValidationError, match='unlicensed_egress_model'):
        ReviewRequest.model_validate(raw)

@pytest.mark.parametrize('kind', ['write_plan','write_preflight','post_execution'])
def test_new_model_permission_still_rejects_production_writes(kind):
    raw = request(M4_SERVER_SCOPE); raw['kind'] = kind
    with pytest.raises(ValidationError, match='server_read_only'):
        ReviewRequest.model_validate(raw)

def test_security_controller_records_actual_new_review_model():
    class Store: pool = object()
    assert SecurityGates(server_cfg(), Store(), object()).controller.model == NEW_MODEL


def test_security_model_and_budget_remain_pinned_for_production():
    from security_agent.config import Config
    cfg = Config('S'*40,'Q'*40,'R'*40,'K'*40,enabled=True,model=NEW_MODEL,runtime_profile='m4-server-readonly')
    cfg.validate()
    for field,value in [('model',OLD_MODEL),('model_timeout',31),('acceptance_timeout_exception',True)]:
        original=getattr(cfg,field);setattr(cfg,field,value)
        with pytest.raises(ValueError):cfg.validate()
        setattr(cfg,field,original)

@pytest.mark.asyncio
async def test_new_signed_review_works_and_old_policy_cannot_invoke_model():
    import base64, json, httpx
    from security_agent.config import Config
    from security_agent.main import create_app
    from review_contract import digest, verify_message
    from test_m4 import headers
    from test_runtime_gates import Provider
    provider=Provider()
    cfg=Config('S'*40,'Q'*40,'R'*40,'K'*40,enabled=True,model=NEW_MODEL,runtime_profile='m4-server-readonly')
    app=create_app(cfg,provider)
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://sec') as client:
        raw=request(M4_SERVER_SCOPE)
        response=await client.post('/internal/ma-review/v1/reviews',json=raw,headers=headers(raw))
        assert response.status_code==200 and len(provider.calls)==1
        metadata=json.loads(base64.urlsafe_b64decode(response.headers['X-SEC-Metadata']))
        review=ReviewRequest.model_validate(raw)
        verify_message(response.headers['X-SEC-Result'],cfg.result_key,'ma-review-result/v1',review.review_id,digest(metadata))
        assert metadata['request_hash']==digest(review.model_dump(mode='json'))
        old=request(M4_SERVER_SCOPE);old.update(policy_version='sec-m4-policy/v5',scope_version='sec-m4-project-readonly/v5')
        response=await client.post('/internal/ma-review/v1/reviews',json=old,headers=headers(old))
        assert response.status_code==403 and len(provider.calls)==1


@pytest.mark.parametrize('changes,expected', [
    ({},'allow'), ({'identity_verified':None},'needs_clarification'),
    ({'ownership_verified':None},'needs_clarification'),
    ({'identity_verified':False},'deny'), ({'args_validated':False},'deny'),
    ({'evidence_sufficient':False},'needs_clarification'),
    ({'identity_verified':None,'args_validated':False},'deny'),
])
def test_program_prerequisites_cannot_be_inferred_from_role_or_asset(changes,expected):
    from security_agent.prerequisites import enforce_prerequisites
    from review_contract import Opinion
    raw=request(M4_SERVER_SCOPE)
    facts={key:True for key in ('identity_verified','ownership_verified','tool_registered','args_validated','audit_ready','target_matches','evidence_sufficient','asset_verified')}
    facts.update(changes);raw['evidence'][0]['facts']=[{'key':k,'value':v} for k,v in facts.items() if v is not None]
    result=enforce_prerequisites(Opinion(decision='allow',reason='fixture model guessed allow',rule_hits=['SEC-R01'],evidence_refs=['P1']),ReviewRequest.model_validate(raw))
    assert result.decision==expected

@pytest.mark.asyncio
async def test_service_signs_program_restriction_when_model_guesses_identity():
    import base64,json,httpx
    from security_agent.config import Config
    from security_agent.main import create_app
    from review_contract import digest,verify_message
    from test_m4 import headers
    from test_runtime_gates import Provider
    provider=Provider();cfg=Config('S'*40,'Q'*40,'R'*40,'K'*40,enabled=True,model=NEW_MODEL,runtime_profile='m4-server-readonly');app=create_app(cfg,provider)
    raw=request(M4_SERVER_SCOPE);raw['evidence'][0]['facts']=[]
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://sec') as client:
        response=await client.post('/internal/ma-review/v1/reviews',json=raw,headers=headers(raw))
        assert response.status_code==200 and response.json()['decision']=='needs_clarification'
        meta=json.loads(base64.urlsafe_b64decode(response.headers['X-SEC-Metadata']))
        verify_message(response.headers['X-SEC-Result'],cfg.result_key,'ma-review-result/v1',ReviewRequest.model_validate(raw).review_id,digest(meta))
        assert meta['decision_hash']==digest(response.json())


def test_explicit_failed_prerequisite_stays_denied_even_if_model_requests_clarification():
    from security_agent.prerequisites import enforce_prerequisites
    from review_contract import Opinion
    raw=request(M4_SERVER_SCOPE);raw['evidence'][0]['facts']=[{'key':'args_validated','value':False}]
    opinion=Opinion(decision='needs_clarification',reason='fixture uncertainty',rule_hits=['SEC-R07'],evidence_refs=['P1'])
    assert enforce_prerequisites(opinion,ReviewRequest.model_validate(raw)).decision=='deny'


@pytest.mark.parametrize('step',[1,2,7])
def test_prerequisites_bind_actual_tool_step_not_fixed_first_step(step):
    from security_agent.prerequisites import enforce_prerequisites
    from review_contract import Opinion
    raw=request(M4_SERVER_SCOPE);raw['step_no']=step
    keys=('identity_verified','ownership_verified','tool_registered','args_validated','audit_ready','target_matches','evidence_sufficient','asset_verified')
    raw['evidence'][0].update(ref='P'+str(step),facts=[{'key':k,'value':True} for k in keys])
    allow=Opinion(decision='allow',reason='valid current step',rule_hits=['SEC-R01'],evidence_refs=['P'+str(step)])
    assert enforce_prerequisites(allow,ReviewRequest.model_validate(raw)).decision=='allow'
    if step>1:
        raw['evidence'][0]['ref']='P1'
        assert enforce_prerequisites(allow,ReviewRequest.model_validate(raw)).decision=='needs_clarification'

@pytest.mark.asyncio
async def test_signed_service_accepts_second_step_prerequisites():
    import httpx
    from security_agent.config import Config
    from security_agent.main import create_app
    from test_m4 import headers
    from test_runtime_gates import Provider
    cfg=Config('S'*40,'Q'*40,'R'*40,'K'*40,enabled=True,model=NEW_MODEL,runtime_profile='m4-server-readonly');app=create_app(cfg,Provider())
    raw=request(M4_SERVER_SCOPE);raw['step_no']=2
    raw['evidence'][0].update(ref='P2',facts=[{'key':k,'value':True} for k in ('identity_verified','ownership_verified','tool_registered','args_validated','audit_ready','target_matches','evidence_sufficient','asset_verified')])
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://sec') as client:
        response=await client.post('/internal/ma-review/v1/reviews',json=raw,headers=headers(raw))
        assert response.status_code==200 and response.json()['decision']=='allow'
