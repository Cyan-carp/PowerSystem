"""S08 schedule review tests: synthetic ASGI app, no real API or service.

Runs without pytest-asyncio so the file stays independent of the stage's
optional async plugin: each case drives one event loop through asyncio.run.
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
from security_agent import schedule_review

REQUEST_KEY = 'Q' * 40
RESULT_KEY = 'R' * 40


def body(**changes):
    raw = {'protocol': 'ma-schedule-review/v1', 'review_id': str(uuid4()), 'binding_hash': 'a' * 64,
           'capability': 'schedule.create', 'data_class': 'internal', 'destination': 'platform_client',
           'risk_class': 'R1', 'owner_id': 987654, 'policy_version': 'v4-s08-local/v1',
           'target_version': 'b' * 64, 'argument_hash': 'c' * 64,
           'expires_at': int(time.time()) + 60}
    raw.update(changes)
    return raw


def headers(raw, key=REQUEST_KEY):
    return {'X-SEC-Request': sign_message(key, 'ma-schedule-review-request/v1',
                                          UUID(raw['review_id']), digest(raw), raw['expires_at'])}


class Model:
    """Records outbound calls and answers with a fixed opinion."""

    def __init__(self, decision='allow', codes=('scope_reviewed',), status=200, failure=None):
        self.decision, self.codes, self.status, self.failure = decision, list(codes), status, failure
        self.calls = []

    async def __call__(self, request):
        self.calls.append({'payload': json.loads(request.content), 'headers': dict(request.headers),
                           'extensions': dict(request.extensions)})
        if self.failure:
            raise self.failure
        if self.status != 200:
            return httpx.Response(self.status, json={'error': 'synthetic'})
        return httpx.Response(200, json={
            'choices': [{'message': {'content': json.dumps({'decision': self.decision,
                                                             'rule_codes': self.codes})}}],
            'usage': {'prompt_tokens': 3, 'completion_tokens': 5, 'total_tokens': 8}})


def build(model=None, profile='v4-s08-local', enabled=True):
    app = FastAPI()
    app.state.cfg = SimpleNamespace(runtime_profile=profile, enabled=enabled, request_key=REQUEST_KEY,
                                    result_key=RESULT_KEY, llm_url='https://api.deepseek.com', llm_key='K' * 40)
    app.state.seen, app.state.active, app.state.users = {}, set(), set()
    app.state.action_client = httpx.AsyncClient(transport=httpx.MockTransport(model or Model()))
    schedule_review.routes(app)
    return app


async def post(app, raw, request_headers=None):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://sec') as client:
        return await client.post('/internal/ma-schedule-review/v1/reviews', json=raw,
                                 headers=headers(raw) if request_headers is None else request_headers)


def run(case):
    asyncio.run(case())


def assert_signed(result, review_id):
    claims = verify_message(result.headers['X-SEC-Result'], RESULT_KEY, 'ma-schedule-review-result/v1',
                            UUID(review_id), digest(result.json()['decision']))
    assert claims['exp'] == result.json()['decision']['expires_at']


@pytest.mark.parametrize('changes', [
    {'capability': 'schedule.delete'},
    {'capability': 'schedule.create', 'risk_class': 'R2'},
    {'capability': 'schedule.create', 'risk_class': 'R0'},
    {'capability': 'schedule.update', 'data_class': 'sensitive'},
    {'capability': 'schedule.resume', 'destination': 'local'},
    {'capability': 'schedule.create', 'destination': 'registered_model'},
    {'capability': 'schedule.create', 'destination': 'external'},
])
def test_hard_deny_never_calls_model(changes):
    async def case():
        model = Model(decision='allow')
        app = build(model)
        raw = body(**changes)
        response = await post(app, raw)
        await app.state.action_client.aclose()
        assert response.status_code == 200
        decision = response.json()['decision']
        assert decision['decision'] == 'deny' and decision['rule_codes'] == ['scope_denied']
        assert set(decision) == {'protocol', 'review_id', 'binding_hash', 'decision', 'rule_codes', 'expires_at'}
        assert not model.calls
        assert_signed(response, raw['review_id'])
    run(case)


@pytest.mark.parametrize('capability', ['schedule.create', 'schedule.update', 'schedule.resume'])
def test_whitelisted_schedule_is_reviewed_then_gated(capability):
    async def case():
        model = Model(decision='allow', codes=['scope_reviewed'])
        app = build(model)
        raw = body(capability=capability)
        response = await post(app, raw)
        await app.state.action_client.aclose()
        assert response.status_code == 200 and len(model.calls) == 1
        assert model.calls[0]['payload']['model'] == 'deepseek-flash'
        decision = response.json()['decision']
        assert decision['decision'] == 'require_approval'
        assert decision['rule_codes'] == ['scope_reviewed', 'approval_required']
        assert_signed(response, raw['review_id'])
    run(case)


def test_model_deny_is_preserved():
    async def case():
        model = Model(decision='deny', codes=['scope_denied'])
        app = build(model)
        raw = body()
        response = await post(app, raw)
        await app.state.action_client.aclose()
        assert response.status_code == 200 and len(model.calls) == 1
        decision = response.json()['decision']
        assert decision['decision'] == 'deny' and decision['rule_codes'] == ['scope_denied']
    run(case)


def test_model_require_approval_is_kept_without_duplicate_code():
    async def case():
        model = Model(decision='require_approval', codes=['approval_required'])
        app = build(model)
        response = await post(app, body())
        await app.state.action_client.aclose()
        assert response.status_code == 200
        decision = response.json()['decision']
        assert decision['decision'] == 'require_approval'
        assert decision['rule_codes'] == ['approval_required']
    run(case)


def test_projection_contains_only_six_metadata_fields():
    async def case():
        model = Model()
        app = build(model)
        raw = body(owner_id=987654, target_version='b' * 64, argument_hash='c' * 64)
        response = await post(app, raw)
        await app.state.action_client.aclose()
        assert response.status_code == 200
        payload = model.calls[0]['payload']
        projected = json.loads(payload['messages'][1]['content'])
        assert set(projected) == {'protocol', 'capability', 'data_class', 'destination', 'risk_class', 'policy_version'}
        sent = json.dumps(payload, ensure_ascii=False)
        assert '987654' not in sent and 'b' * 64 not in sent and 'c' * 64 not in sent
        assert 'owner' not in sent and 'argument_hash' not in sent and 'target_version' not in sent
        assert payload['thinking'] == {'type': 'disabled'} and payload['stream'] is True
        assert payload['response_format'] == {'type': 'json_object'}
        assert 'tools' not in payload
        assert model.calls[0]['headers'].get('authorization') == 'Bearer ' + 'K' * 40
    run(case)


def test_model_outage_blocks_without_retry():
    async def case():
        model = Model(failure=httpx.TimeoutException('synthetic'))
        app = build(model)
        response = await post(app, body())
        await app.state.action_client.aclose()
        assert response.status_code == 503 and len(model.calls) == 1
        assert 'X-SEC-Result' not in response.headers
    run(case)


@pytest.mark.parametrize('decision,codes', [
    ('maybe', ['scope_reviewed']),
    ('allow', ['unlicensed_code']),
    ('allow', ['scope_reviewed', 'scope_reviewed']),
])
def test_invalid_model_opinion_is_unavailable(decision, codes):
    async def case():
        app = build(Model(decision=decision, codes=codes))
        response = await post(app, body())
        await app.state.action_client.aclose()
        assert response.status_code == 503 and 'X-SEC-Result' not in response.headers
    run(case)


def test_http_error_from_model_is_unavailable():
    async def case():
        app = build(Model(status=500))
        response = await post(app, body())
        await app.state.action_client.aclose()
        assert response.status_code == 503
    run(case)


def test_disabled_or_foreign_profile_blocks_before_model():
    async def case():
        for profile, enabled in (('v4-s08-local', False), ('v4-s07-local', True), ('legacy', True)):
            model = Model()
            app = build(model, profile=profile, enabled=enabled)
            response = await post(app, body())
            await app.state.action_client.aclose()
            assert response.status_code == 503 and not model.calls
    run(case)


def test_foreign_protocol_and_policy_are_rejected():
    async def case():
        app = build(Model())
        assert (await post(app, body(protocol='ma-ops-review/v1'))).status_code in (400, 422)
        assert (await post(app, body(policy_version='v4-s07-local/v1'))).status_code in (400, 422)
        await app.state.action_client.aclose()
    run(case)


def test_unknown_field_and_bad_signature_rejected():
    async def case():
        app = build(Model())
        raw = body()
        raw['schedule'] = 'raw material'
        assert (await post(app, raw)).status_code in (400, 422)
        valid = body()
        assert (await post(app, valid, {'X-SEC-Request': sign_message(
            'W' * 40, 'ma-schedule-review-request/v1', UUID(valid['review_id']), digest(valid),
            valid['expires_at'])})).status_code == 403
        # A signature minted for the S07 audience must not authorise S08.
        assert (await post(app, valid, {'X-SEC-Request': sign_message(
            REQUEST_KEY, 'ma-ops-review-request/v1', UUID(valid['review_id']), digest(valid),
            valid['expires_at'])})).status_code == 403
        token = sign_message(REQUEST_KEY, 'ma-schedule-review-request/v1', UUID(valid['review_id']),
                             digest(valid), valid['expires_at'])
        tampered = token[:-1] + ('0' if token[-1] != '0' else '1')
        assert (await post(app, valid, {'X-SEC-Request': tampered})).status_code == 403
        await app.state.action_client.aclose()
    run(case)


def test_stale_and_overlong_expiry_are_rejected():
    async def case():
        # The envelope already bounds iat/exp, so an expired or overlong window
        # fails signature verification before the route's own staleness check.
        app = build(Model())
        now = int(time.time())
        assert (await post(app, body(expires_at=now - 1))).status_code == 403
        assert (await post(app, body(expires_at=now + 400))).status_code == 403
        await app.state.action_client.aclose()
    run(case)


def test_replay_is_rejected_and_capacity_released():
    async def case():
        app = build(Model())
        raw = body()
        first = await post(app, raw)
        second = await post(app, raw)
        await app.state.action_client.aclose()
        assert first.status_code == 200 and second.status_code == 409
        assert not app.state.active and not app.state.users
    run(case)
