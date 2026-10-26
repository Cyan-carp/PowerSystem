"""S06/S08 production action profile tests: synthetic ASGI app and config only.

No network, no real credential file, no production system and no model call is
involved: every outbound request is captured by ``httpx.MockTransport`` and every
secret used by ``Config.load`` is a synthetic value written to a temp directory.

Covered boundaries:

* the explicit ``V4_PRODUCTION_PROFILE`` resolves both new domains to their own
  production policy, and a foreign policy literal is refused before the model;
* the production domain uses the independent official action URL/key/model,
  while local and joint profiles keep the legacy local pair with no fallback;
* an ``m4-server-readonly`` agent without the new profile still rejects the new
  routes without calling the model;
* secret/external requests are denied deterministically, R1 allow opinions are
  escalated to a human approval, and an unavailable model fails closed;
* no credential reaches the outbound projection body.

Each case drives one event loop through ``asyncio.run`` so it does not depend on
the optional async pytest plugin.
"""
import asyncio
import json
import time
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI

from review_contract import digest, sign_message, verify_message
from security_agent import action_review, schedule_review
from security_agent.config import Config
from security_agent.production_profile import (
    ACTION_LOCAL_POLICY,
    ACTION_PRODUCTION_POLICY,
    SCHEDULE_LOCAL_POLICY,
    SCHEDULE_PRODUCTION_POLICY,
    V4_PRODUCTION_PROFILE,
    action_domain,
    model_access,
    production,
    schedule_domain,
)

REQUEST_KEY = 'Q' * 40
RESULT_KEY = 'R' * 40
LOCAL_KEY = 'L' * 40
ACTION_KEY = 'A' * 40
ACTION_URL = 'https://api.deepseek.com'
LEGACY_URL = 'https://hgapi.dieqiyun.top/v1'

ACTION_PATH = '/internal/ma-action-review/v1/reviews'
SCHEDULE_PATH = '/internal/ma-schedule-review/v1/reviews'
ACTION_AUDIENCE = 'ma-action-review-request/v1'
SCHEDULE_AUDIENCE = 'ma-schedule-review-request/v1'

PRODUCTION_ENV = (
    'SEC_ENABLED', 'SEC_SERVICE_TOKEN_FILE', 'SEC_REQUEST_KEY_FILE', 'SEC_RESULT_KEY_FILE',
    'SEC_LLM_API_KEY_FILE', 'SEC_LLM_URL', 'SEC_MODEL', 'SEC_MODEL_TIMEOUT_SECONDS',
    'SEC_REVIEW_TTL_SECONDS', 'SEC_ACCEPTANCE_TIMEOUT_EXCEPTION_ENABLED',
    'SEC_RUNTIME_PROFILE', 'SEC_ACTION_LLM_API_KEY_FILE', 'SEC_ACTION_LLM_URL',
    'SEC_ACTION_MODEL', 'V4_PRODUCTION_PROFILE', 'SEC_PRODUCTION_PROFILE',
    'V4_S09_LOCAL_ENABLED',
)


# --------------------------------------------------------------------------- #
# Synthetic route-level harness
# --------------------------------------------------------------------------- #

def action_body(**changes):
    raw = {'protocol': 'ma-action-review/v1', 'review_id': str(uuid4()), 'binding_hash': 'b' * 64,
           'capability': 'alarm.ack', 'data_class': 'internal', 'destination': 'platform_client',
           'risk_class': 'R1', 'owner_id': 987654, 'policy_version': ACTION_LOCAL_POLICY,
           'target_version': 'd' * 16, 'argument_hash': 'c' * 64, 'remark_length': 12,
           'expires_at': int(time.time()) + 60}
    raw.update(changes)
    return raw


def schedule_body(**changes):
    raw = {'protocol': 'ma-schedule-review/v1', 'review_id': str(uuid4()), 'binding_hash': 'b' * 64,
           'capability': 'schedule.create', 'data_class': 'internal',
           'destination': 'platform_client', 'risk_class': 'R1', 'owner_id': 987654,
           'policy_version': SCHEDULE_LOCAL_POLICY, 'target_version': 'e' * 64,
           'argument_hash': 'c' * 64, 'expires_at': int(time.time()) + 60}
    raw.update(changes)
    return raw


def action_token(raw, key=REQUEST_KEY):
    return sign_message(key, ACTION_AUDIENCE, UUID(raw['review_id']), digest(raw), raw['expires_at'])


def schedule_token(raw, key=REQUEST_KEY):
    return sign_message(key, SCHEDULE_AUDIENCE, UUID(raw['review_id']), digest(raw), raw['expires_at'])


class Model:
    """Records every outbound request and answers with a fixed synthetic body."""

    def __init__(self, decision='allow', codes=('scope_reviewed',), status=200, failure=None,
                 content=None):
        self.decision, self.codes, self.status = decision, list(codes), status
        self.failure, self.content, self.calls = failure, content, []

    async def __call__(self, request):
        self.calls.append({'url': str(request.url), 'payload': json.loads(request.content),
                           'headers': dict(request.headers)})
        if self.failure is not None:
            raise self.failure
        if self.status != 200:
            return httpx.Response(self.status, json={'error': 'synthetic'})
        content = self.content if self.content is not None else json.dumps(
            {'decision': self.decision, 'rule_codes': self.codes})
        return httpx.Response(200, json={
            'choices': [{'message': {'content': content}}],
            'usage': {'prompt_tokens': 3, 'completion_tokens': 5, 'total_tokens': 8}})


def build(model, **overrides):
    settings = dict(runtime_profile='m4-server-readonly', production_profile=V4_PRODUCTION_PROFILE,
                    enabled=True, request_key=REQUEST_KEY, result_key=RESULT_KEY,
                    llm_url=LEGACY_URL, llm_key=LOCAL_KEY, action_llm_url=ACTION_URL,
                    action_llm_key=ACTION_KEY, action_model='deepseek-flash')
    settings.update(overrides)
    app = FastAPI()
    app.state.cfg = SimpleNamespace(**settings)
    app.state.seen, app.state.active, app.state.users = {}, set(), set()
    app.state.action_client = httpx.AsyncClient(transport=httpx.MockTransport(model))
    action_review.routes(app)
    schedule_review.routes(app)
    return app


async def post(app, path, raw, token):
    headers = {'X-SEC-Request': token} if token is not None else {}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url='http://sec') as client:
        return await client.post(path, json=raw, headers=headers)


def run(case):
    asyncio.run(case())


def assert_signed(response, review_id, audience='ma-action-review-result/v1'):
    claims = verify_message(response.headers['X-SEC-Result'], RESULT_KEY, audience,
                            UUID(review_id), digest(response.json()['decision']))
    assert claims['exp'] == response.json()['decision']['expires_at']


def assert_released(app):
    assert not app.state.active and not app.state.users


# --------------------------------------------------------------------------- #
# Domain resolution
# --------------------------------------------------------------------------- #

def test_production_domains_resolve_their_own_policies():
    cfg = SimpleNamespace(production_profile=V4_PRODUCTION_PROFILE,
                          runtime_profile='m4-server-readonly')
    assert production(cfg) is True
    assert action_domain(cfg) == ('production', ACTION_PRODUCTION_POLICY)
    assert schedule_domain(cfg) == ('production', SCHEDULE_PRODUCTION_POLICY)
    assert (ACTION_PRODUCTION_POLICY, SCHEDULE_PRODUCTION_POLICY) == (
        'v4-s06-production/v1', 'v4-s08-production/v1')
    # The explicit production profile outranks any local candidate runtime name.
    cfg.runtime_profile = 'v4-s09-local'
    assert action_domain(cfg) == ('production', ACTION_PRODUCTION_POLICY)
    assert schedule_domain(cfg) == ('production', SCHEDULE_PRODUCTION_POLICY)


@pytest.mark.parametrize('profile', ['v4-s06-local', 'v4-s09-local'])
def test_action_local_and_joint_keep_local_policy(profile):
    cfg = SimpleNamespace(production_profile='', runtime_profile=profile)
    assert action_domain(cfg) == ('local', ACTION_LOCAL_POLICY)


@pytest.mark.parametrize('profile', ['v4-s08-local', 'v4-s09-local'])
def test_schedule_local_and_joint_keep_local_policy(profile):
    cfg = SimpleNamespace(production_profile='', runtime_profile=profile)
    assert schedule_domain(cfg) == ('local', SCHEDULE_LOCAL_POLICY)


@pytest.mark.parametrize('profile', ['legacy', 'm4-local', 'm4-server-readonly', 'v4-s07-local'])
def test_unknown_or_unrelated_profile_keeps_new_domains_closed(profile):
    cfg = SimpleNamespace(production_profile='', runtime_profile=profile)
    assert production(cfg) is False
    assert action_domain(cfg) is None
    assert schedule_domain(cfg) is None


@pytest.mark.parametrize('value', ['v4-business-schedule/v2', 'v4_production', ' m4-server-readonly'])
def test_production_match_is_exact(value):
    assert production(SimpleNamespace(production_profile=value)) is False
    assert action_domain(SimpleNamespace(production_profile=value,
                                         runtime_profile='m4-server-readonly')) is None


def test_model_access_has_no_implicit_fallback():
    cfg = SimpleNamespace(llm_url=LEGACY_URL, llm_key=LOCAL_KEY, action_llm_url=ACTION_URL,
                          action_llm_key=ACTION_KEY, action_model='deepseek-flash')
    assert model_access(cfg, 'production') == {
        'url': ACTION_URL, 'key': ACTION_KEY, 'model': 'deepseek-flash'}
    assert model_access(cfg, 'local') == {
        'url': LEGACY_URL, 'key': LOCAL_KEY, 'model': 'deepseek-flash'}
    assert model_access(cfg, 'local')['key'] != model_access(cfg, 'production')['key']


# --------------------------------------------------------------------------- #
# S06 production route
# --------------------------------------------------------------------------- #

def test_s06_production_uses_official_action_channel():
    async def case():
        model = Model()
        app = build(model)
        raw = action_body(policy_version=ACTION_PRODUCTION_POLICY)
        response = await post(app, ACTION_PATH, raw, action_token(raw))
        await app.state.action_client.aclose()
        assert response.status_code == 200, response.text
        assert len(model.calls) == 1
        call = model.calls[0]
        assert call['url'] == ACTION_URL + '/chat/completions'
        assert call['headers']['authorization'] == 'Bearer ' + ACTION_KEY
        assert call['payload']['model'] == 'deepseek-flash'
        decision = response.json()['decision']
        assert decision['decision'] == 'require_approval'
        assert decision['rule_codes'] == ['scope_reviewed', 'approval_required']
        assert_signed(response, raw['review_id'])
        assert_released(app)
    run(case)


@pytest.mark.parametrize('policy,status', [
    (ACTION_LOCAL_POLICY, 403),
    (SCHEDULE_PRODUCTION_POLICY, 422),
])
def test_s06_production_rejects_foreign_policy_before_model(policy, status):
    async def case():
        model = Model()
        app = build(model)
        raw = action_body(policy_version=policy)
        response = await post(app, ACTION_PATH, raw, action_token(raw))
        await app.state.action_client.aclose()
        assert response.status_code == status
        if status == 403:
            assert response.json()['detail'] == 'security_policy_profile_mismatch'
        assert not model.calls
        assert 'X-SEC-Result' not in response.headers
    run(case)


def test_s06_local_profile_rejects_production_policy():
    async def case():
        model = Model()
        app = build(model, runtime_profile='v4-s06-local', production_profile='',
                    llm_url=ACTION_URL)
        raw = action_body(policy_version=ACTION_PRODUCTION_POLICY)
        response = await post(app, ACTION_PATH, raw, action_token(raw))
        await app.state.action_client.aclose()
        assert response.status_code == 403
        assert response.json()['detail'] == 'security_policy_profile_mismatch'
        assert not model.calls
    run(case)


# --------------------------------------------------------------------------- #
# S08 production route
# --------------------------------------------------------------------------- #

def test_s08_production_uses_official_action_channel():
    async def case():
        model = Model()
        app = build(model)
        raw = schedule_body(policy_version=SCHEDULE_PRODUCTION_POLICY)
        response = await post(app, SCHEDULE_PATH, raw, schedule_token(raw))
        await app.state.action_client.aclose()
        assert response.status_code == 200, response.text
        assert len(model.calls) == 1
        call = model.calls[0]
        assert call['url'] == ACTION_URL + '/chat/completions'
        assert call['headers']['authorization'] == 'Bearer ' + ACTION_KEY
        decision = response.json()['decision']
        assert decision['decision'] == 'require_approval'
        assert decision['rule_codes'] == ['scope_reviewed', 'approval_required']
        assert_signed(response, raw['review_id'], 'ma-schedule-review-result/v1')
        assert_released(app)
    run(case)


@pytest.mark.parametrize('policy,status', [
    (SCHEDULE_LOCAL_POLICY, 403),
    (ACTION_PRODUCTION_POLICY, 422),
])
def test_s08_production_rejects_foreign_policy_before_model(policy, status):
    async def case():
        model = Model()
        app = build(model)
        raw = schedule_body(policy_version=policy)
        response = await post(app, SCHEDULE_PATH, raw, schedule_token(raw))
        await app.state.action_client.aclose()
        assert response.status_code == status
        if status == 403:
            assert response.json()['detail'] == 'security_policy_profile_mismatch'
        assert not model.calls
    run(case)


# --------------------------------------------------------------------------- #
# Legacy m4 keeps the new routes closed
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize('profile', ['m4-server-readonly', 'legacy', 'm4-local'])
def test_old_m4_without_production_profile_rejects_both_routes(profile):
    async def case():
        model = Model()
        app = build(model, runtime_profile=profile, production_profile='')
        action_raw = action_body(policy_version=ACTION_PRODUCTION_POLICY)
        schedule_raw = schedule_body(policy_version=SCHEDULE_PRODUCTION_POLICY)
        first = await post(app, ACTION_PATH, action_raw, action_token(action_raw))
        second = await post(app, SCHEDULE_PATH, schedule_raw, schedule_token(schedule_raw))
        await app.state.action_client.aclose()
        assert first.status_code == 503 and first.json()['detail'] == 's06_review_disabled'
        assert second.status_code == 503 and second.json()['detail'] == 's08_review_disabled'
        assert not model.calls
        assert 'X-SEC-Result' not in first.headers and 'X-SEC-Result' not in second.headers
    run(case)


def test_local_profiles_do_not_open_the_other_domain():
    async def case():
        model = Model()
        action_only = build(model, runtime_profile='v4-s06-local', production_profile='',
                            llm_url=ACTION_URL)
        schedule_only = build(model, runtime_profile='v4-s08-local', production_profile='',
                              llm_url=ACTION_URL)
        schedule_raw = schedule_body(policy_version=SCHEDULE_PRODUCTION_POLICY)
        action_raw = action_body(policy_version=ACTION_PRODUCTION_POLICY)
        blocked_schedule = await post(action_only, SCHEDULE_PATH, schedule_raw,
                                      schedule_token(schedule_raw))
        blocked_action = await post(schedule_only, ACTION_PATH, action_raw,
                                    action_token(action_raw))
        await action_only.state.action_client.aclose()
        await schedule_only.state.action_client.aclose()
        assert blocked_schedule.status_code == 503
        assert blocked_schedule.json()['detail'] == 's08_review_disabled'
        assert blocked_action.status_code == 503
        assert blocked_action.json()['detail'] == 's06_review_disabled'
        assert not model.calls
    run(case)


def test_disabled_production_agent_blocks_before_model():
    async def case():
        model = Model()
        app = build(model, enabled=False)
        raw = action_body(policy_version=ACTION_PRODUCTION_POLICY)
        response = await post(app, ACTION_PATH, raw, action_token(raw))
        await app.state.action_client.aclose()
        assert response.status_code == 503 and not model.calls
    run(case)


# --------------------------------------------------------------------------- #
# Local / joint routing stays on the local credential
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize('profile', ['v4-s06-local', 'v4-s09-local'])
def test_local_domain_never_reaches_action_credential(profile):
    async def case():
        model = Model()
        app = build(model, runtime_profile=profile, production_profile='', llm_url=ACTION_URL)
        raw = action_body(policy_version=ACTION_LOCAL_POLICY)
        response = await post(app, ACTION_PATH, raw, action_token(raw))
        await app.state.action_client.aclose()
        assert response.status_code == 200
        call = model.calls[0]
        assert call['url'] == ACTION_URL + '/chat/completions'
        assert call['headers']['authorization'] == 'Bearer ' + LOCAL_KEY
        assert ACTION_KEY not in json.dumps(call['payload'], ensure_ascii=False)
    run(case)


@pytest.mark.parametrize('profile', ['v4-s08-local', 'v4-s09-local'])
def test_schedule_local_domain_never_reaches_action_credential(profile):
    async def case():
        model = Model()
        app = build(model, runtime_profile=profile, production_profile='', llm_url=ACTION_URL)
        raw = schedule_body(policy_version=SCHEDULE_LOCAL_POLICY)
        response = await post(app, SCHEDULE_PATH, raw, schedule_token(raw))
        await app.state.action_client.aclose()
        assert response.status_code == 200
        assert model.calls[0]['headers']['authorization'] == 'Bearer ' + LOCAL_KEY
        assert ACTION_KEY not in json.dumps(model.calls[0]['payload'], ensure_ascii=False)
    run(case)


def test_production_never_sends_legacy_local_credential():
    async def case():
        model = Model()
        app = build(model)
        raw = action_body(policy_version=ACTION_PRODUCTION_POLICY)
        response = await post(app, ACTION_PATH, raw, action_token(raw))
        await app.state.action_client.aclose()
        assert response.status_code == 200
        sent = json.dumps({'payload': model.calls[0]['payload'],
                           'headers': model.calls[0]['headers']}, ensure_ascii=False)
        assert LOCAL_KEY not in sent
    run(case)


# --------------------------------------------------------------------------- #
# Deterministic deny, approval gate, fail closed
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize('changes', [
    {'data_class': 'secret'},
    {'destination': 'external'},
    {'destination': 'registered_model'},
    {'capability': 'device.delete'},
    {'capability': 'alarm.ack', 'risk_class': 'R2'},
    {'capability': 'device.remark.read', 'data_class': 'internal'},
    {'capability': 'device.remark.read', 'risk_class': 'R1'},
])
def test_s06_hard_deny_never_calls_model(changes):
    async def case():
        model = Model(decision='allow')
        app = build(model)
        raw = action_body(policy_version=ACTION_PRODUCTION_POLICY, **changes)
        response = await post(app, ACTION_PATH, raw, action_token(raw))
        await app.state.action_client.aclose()
        assert response.status_code == 200
        decision = response.json()['decision']
        assert decision['decision'] == 'deny' and decision['rule_codes'] == ['deterministic_deny']
        assert not model.calls
        assert_signed(response, raw['review_id'])
    run(case)


def test_s06_r1_model_allow_requires_human_approval():
    async def case():
        model = Model(decision='allow', codes=['scope_reviewed'])
        app = build(model)
        raw = action_body(policy_version=ACTION_PRODUCTION_POLICY)
        response = await post(app, ACTION_PATH, raw, action_token(raw))
        await app.state.action_client.aclose()
        decision = response.json()['decision']
        assert decision['decision'] == 'require_approval'
        assert decision['rule_codes'] == ['scope_reviewed', 'approval_required']
    run(case)


def test_s06_model_deny_is_never_relaxed():
    async def case():
        model = Model(decision='deny', codes=['scope_denied'])
        app = build(model)
        raw = action_body(policy_version=ACTION_PRODUCTION_POLICY)
        response = await post(app, ACTION_PATH, raw, action_token(raw))
        await app.state.action_client.aclose()
        decision = response.json()['decision']
        assert decision['decision'] == 'deny' and decision['rule_codes'] == ['scope_denied']
    run(case)


def test_s08_r1_model_allow_requires_human_approval():
    async def case():
        model = Model(decision='allow', codes=['scope_reviewed'])
        app = build(model)
        raw = schedule_body(policy_version=SCHEDULE_PRODUCTION_POLICY)
        response = await post(app, SCHEDULE_PATH, raw, schedule_token(raw))
        await app.state.action_client.aclose()
        decision = response.json()['decision']
        assert decision['decision'] == 'require_approval'
        assert decision['rule_codes'] == ['scope_reviewed', 'approval_required']
    run(case)


@pytest.mark.parametrize('failure', [
    None,
    httpx.TimeoutException('synthetic'),
    httpx.ConnectError('synthetic'),
])
def test_model_outage_fails_closed_without_retry(failure):
    async def case():
        model = Model(status=500) if failure is None else Model(failure=failure)
        app = build(model)
        raw = schedule_body(policy_version=SCHEDULE_PRODUCTION_POLICY)
        response = await post(app, SCHEDULE_PATH, raw, schedule_token(raw))
        await app.state.action_client.aclose()
        assert response.status_code == 503
        assert response.json()['detail'] in ('security_model_unavailable', 'security_unavailable')
        assert 'X-SEC-Result' not in response.headers
        assert len(model.calls) == 1
        assert_released(app)
    run(case)


@pytest.mark.parametrize('content', [
    '{"decision":"allow"}',
    '{"decision":"allow","rule_codes":["scope_reviewed"],"extra":1}',
    '{"decision":"maybe","rule_codes":["scope_reviewed"]}',
    '{"decision":"allow","rule_codes":["bogus_code"]}',
    '{"decision":"allow","rule_codes":["scope_reviewed","scope_reviewed"]}',
    '{"decision":"allow","rule_codes":["scope_reviewed","scope_denied","approval_required","scope_denied"]}',
    'not json',
])
def test_invalid_model_opinion_fails_closed(content):
    async def case():
        model = Model(content=content)
        app = build(model)
        raw = schedule_body(policy_version=SCHEDULE_PRODUCTION_POLICY)
        response = await post(app, SCHEDULE_PATH, raw, schedule_token(raw))
        await app.state.action_client.aclose()
        assert response.status_code == 503
        assert response.json()['detail'] in ('security_invalid_opinion', 'security_unavailable')
        assert 'X-SEC-Result' not in response.headers
        assert_released(app)
    run(case)


# --------------------------------------------------------------------------- #
# Outbound projection minimalisation
# --------------------------------------------------------------------------- #

def test_action_projection_excludes_identity_and_credentials():
    async def case():
        model = Model()
        app = build(model)
        raw = action_body(policy_version=ACTION_PRODUCTION_POLICY, owner_id=987654,
                          remark_length=42)
        response = await post(app, ACTION_PATH, raw, action_token(raw))
        await app.state.action_client.aclose()
        assert response.status_code == 200
        payload = model.calls[0]['payload']
        projected = json.loads(payload['messages'][1]['content'])
        assert set(projected) == {'protocol', 'capability', 'data_class', 'destination',
                                  'risk_class', 'policy_version', 'remark_length'}
        sent = json.dumps(payload, ensure_ascii=False)
        assert '987654' not in sent and 'b' * 64 not in sent and 'c' * 64 not in sent
        assert 'd' * 16 not in sent
        for field in ('review_id', 'binding_hash', 'owner_id', 'target_version', 'argument_hash'):
            assert field not in sent
        for secret in (ACTION_KEY, LOCAL_KEY, REQUEST_KEY, RESULT_KEY):
            assert secret not in sent
        assert payload['thinking'] == {'type': 'disabled'} and payload['stream'] is True
        assert payload['response_format'] == {'type': 'json_object'}
        assert 'tools' not in payload
    run(case)


def test_schedule_projection_excludes_identity_and_credentials():
    async def case():
        model = Model()
        app = build(model)
        raw = schedule_body(policy_version=SCHEDULE_PRODUCTION_POLICY, owner_id=987654)
        response = await post(app, SCHEDULE_PATH, raw, schedule_token(raw))
        await app.state.action_client.aclose()
        assert response.status_code == 200
        payload = model.calls[0]['payload']
        projected = json.loads(payload['messages'][1]['content'])
        assert set(projected) == {'protocol', 'capability', 'data_class', 'destination',
                                  'risk_class', 'policy_version'}
        sent = json.dumps(payload, ensure_ascii=False)
        assert '987654' not in sent and 'b' * 64 not in sent and 'c' * 64 not in sent
        assert 'e' * 64 not in sent
        for secret in (ACTION_KEY, LOCAL_KEY, REQUEST_KEY, RESULT_KEY):
            assert secret not in sent
    run(case)


# --------------------------------------------------------------------------- #
# Config.load with synthetic secret files
# --------------------------------------------------------------------------- #

def synthetic_env(monkeypatch, tmp_path, **overrides):
    """Write synthetic credentials to tmp files and set the V4 environment."""
    for name in PRODUCTION_ENV:
        monkeypatch.delenv(name, raising=False)
    values = {'service': 'S' * 40, 'request': 'Q' * 40, 'result': 'R' * 40,
              'llm': 'L' * 40, 'action': 'A' * 40}
    paths = {}
    for name, value in values.items():
        path = tmp_path / f'{name}-synthetic.key'
        path.write_text(value + '\n', encoding='utf-8')
        paths[name] = path
    settings = {
        'SEC_ENABLED': 'true',
        'SEC_SERVICE_TOKEN_FILE': str(paths['service']),
        'SEC_REQUEST_KEY_FILE': str(paths['request']),
        'SEC_RESULT_KEY_FILE': str(paths['result']),
        'SEC_LLM_API_KEY_FILE': str(paths['llm']),
        'SEC_LLM_URL': LEGACY_URL,
        'SEC_MODEL': 'deepseek-v4.1-flash',
        'SEC_RUNTIME_PROFILE': 'm4-server-readonly',
        'SEC_ACTION_LLM_API_KEY_FILE': str(paths['action']),
        'SEC_ACTION_LLM_URL': ACTION_URL,
        'SEC_ACTION_MODEL': 'deepseek-flash',
        'V4_PRODUCTION_PROFILE': V4_PRODUCTION_PROFILE,
    }
    settings.update({key: str(value) for key, value in overrides.items()})
    for key, value in settings.items():
        monkeypatch.setenv(key, value)
    return paths


def test_config_load_accepts_explicit_v4_production_profile(monkeypatch, tmp_path):
    synthetic_env(monkeypatch, tmp_path)
    cfg = Config.load()
    assert cfg.runtime_profile == 'm4-server-readonly'
    assert cfg.production_profile == V4_PRODUCTION_PROFILE
    assert cfg.model == 'deepseek-v4.1-flash'
    assert cfg.action_llm_url == ACTION_URL and cfg.action_model == 'deepseek-flash'
    assert cfg.action_llm_key == 'A' * 40 and cfg.llm_key == 'L' * 40
    assert production(cfg) is True
    assert action_domain(cfg) == ('production', ACTION_PRODUCTION_POLICY)
    assert schedule_domain(cfg) == ('production', SCHEDULE_PRODUCTION_POLICY)
    # Whichever credential is selected, the other one never appears in the loggable repr.
    text = repr(cfg)
    assert 'A' * 40 not in text and 'L' * 40 not in text and 'Q' * 40 not in text


def test_config_load_ignores_legacy_sec_production_profile(monkeypatch, tmp_path):
    synthetic_env(monkeypatch, tmp_path, V4_PRODUCTION_PROFILE='')
    monkeypatch.setenv('SEC_PRODUCTION_PROFILE', V4_PRODUCTION_PROFILE)
    cfg = Config.load()
    assert cfg.production_profile == ''
    assert production(cfg) is False
    assert action_domain(cfg) is None and schedule_domain(cfg) is None


@pytest.mark.parametrize('value', ['v4-business-schedule/v2', 'v4-business-schedule',
                                   'V4-BUSINESS-SCHEDULE/V1', ' v4-business-schedule/v1'])
def test_config_load_rejects_unknown_production_profile(monkeypatch, tmp_path, value):
    synthetic_env(monkeypatch, tmp_path, V4_PRODUCTION_PROFILE=value)
    with pytest.raises(ValueError, match='unknown_production_profile'):
        Config.load()


def test_config_load_requires_m4_runtime_for_production(monkeypatch, tmp_path):
    synthetic_env(monkeypatch, tmp_path, SEC_RUNTIME_PROFILE='v4-s08-local',
                  SEC_LLM_URL=ACTION_URL, SEC_MODEL='deepseek-flash')
    with pytest.raises(ValueError, match='production_profile_requires_m4_server_readonly'):
        Config.load()


@pytest.mark.parametrize('overrides, error', [
    ({'SEC_ACTION_LLM_URL': ACTION_URL + '/v1'}, 'production_action_endpoint_required'),
    ({'SEC_ACTION_LLM_URL': 'https://user@api.deepseek.com'}, 'production_action_endpoint_required'),
    ({'SEC_ACTION_MODEL': 'deepseek-chat'}, 'production_action_model_required'),
])
def test_config_load_rejects_non_official_action_channel(monkeypatch, tmp_path, overrides, error):
    synthetic_env(monkeypatch, tmp_path, **overrides)
    with pytest.raises(ValueError, match=error):
        Config.load()


def test_config_load_rejects_short_action_credential(monkeypatch, tmp_path):
    synthetic_env(monkeypatch, tmp_path)
    short = tmp_path / 'action-short-synthetic.key'
    short.write_text('A' * 20 + '\n', encoding='utf-8')
    monkeypatch.setenv('SEC_ACTION_LLM_API_KEY_FILE', str(short))
    with pytest.raises(ValueError, match='production_action_credentials_required'):
        Config.load()


def test_config_load_requires_independent_action_credential(monkeypatch, tmp_path):
    paths = synthetic_env(monkeypatch, tmp_path)
    # Same file, then same content in a different file: both are one credential.
    monkeypatch.setenv('SEC_ACTION_LLM_API_KEY_FILE', str(paths['llm']))
    with pytest.raises(ValueError, match='production_action_credentials_not_independent'):
        Config.load()
    copy = tmp_path / 'action-copy-synthetic.key'
    copy.write_text('L' * 40 + '\n', encoding='utf-8')
    monkeypatch.setenv('SEC_ACTION_LLM_API_KEY_FILE', str(copy))
    with pytest.raises(ValueError, match='production_action_credentials_not_independent'):
        Config.load()
