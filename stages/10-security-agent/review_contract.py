"""Strict ma-review/v1 messages shared with the program controller."""
import base64
import hashlib
import hmac
import json
import math
import re
import time
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr, field_validator, model_validator

PROTOCOL = 'ma-review/v1'
POLICY = 'sec-m1-policy/v6'
SCOPE = 'sec-m1-synthetic/v3'
RUNTIME_POLICY = 'sec-m2-policy/v5'
RUNTIME_SCOPE = 'sec-m2-local-drill/v1'
INSPECTION_POLICY = 'sec-m3-policy/v3'
INSPECTION_SCOPE = 'sec-m3-local-inspection/v1'
M4_POLICY = 'sec-m4-policy/v2'
M4_LOCAL_SCOPE = 'sec-m4-local-integration/v2'
M4_SERVER_POLICY_V3 = 'sec-m4-policy/v3'
M4_SERVER_POLICY_V4 = 'sec-m4-policy/v4'
M4_SERVER_POLICY = 'sec-m4-policy/v5'
M4_SERVER_SCOPE_PREVIOUS = 'sec-m4-project-readonly/v2'
M4_SERVER_SCOPE_V3 = 'sec-m4-project-readonly/v3'
M4_SERVER_SCOPE_V4 = 'sec-m4-project-readonly/v4'
M4_SERVER_SCOPE = 'sec-m4-project-readonly/v5'
RULES = tuple(f'SEC-R{i:02}' for i in range(1, 13))
KINDS = Literal['sensitive_read', 'data_egress', 'write_plan', 'write_preflight', 'post_execution', 'inspection_report']
HASH = r'^[a-f0-9]{64}$'
FACT_KEYS = {'status', 'project_scope', 'criticality', 'explicit_execution', 'approval_status',
             'execution_status', 'asset_verified', 'log_filtered', 'checked_items', 'not_checked_items',
             'cpu_percent', 'memory_percent', 'target_matches', 'evidence_sufficient', 'restorable',
             'identity_verified','ownership_verified','tool_registered','args_validated','audit_ready',
             'approval_current_epoch','approval_expires_at','approval_valid','plan_bypasses_approval'}
M1_FACT_KEYS = frozenset(FACT_KEYS)
FACT_KEYS |= {'inspection_rule','inspection_status','inspection_reason','coverage_limited',
              'running', 'health', 'image_digest', 'snapshot_hash', 'material_hash',
              'egress_permitted', 'plan_unchanged', 'policy_current', 'event_count', 'connection_count', 'registry_verified', 'payload_bound', 'approval_consumed'}
SECRET = re.compile(r'(?i)\bBearer\s+\S+|\bsk-[a-z0-9_-]{16,}|eyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+|-----BEGIN [A-Z ]*PRIVATE KEY-----|(?:password|api_key|secret|authorization|cookie)\s*[:=]\s*\S+|(?:postgres(?:ql)?|redis)://[^\s]+')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def safe_text(value, secrets=()):
    if SECRET.search(value) or any(secret and secret in value for secret in secrets):
        raise ValueError('sensitive_material_blocked')
    return value


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class Actor(Strict):
    user_id: int = Field(gt=0, strict=True)
    role: Literal['operator', 'admin']


class Tool(Strict):
    name: str = Field(min_length=1, max_length=100, pattern=r'^[a-z][a-z0-9_.]+$')
    version: str = Field(min_length=1, max_length=40)
    risk_class: Literal['R0', 'R2']
    args_hash: str = Field(pattern=HASH)
    plan_hash: str = Field(pattern=HASH)


class Asset(Strict):
    asset_id: str = Field(min_length=1, max_length=100, pattern=r'^[a-zA-Z0-9_.-]+$')
    version: int = Field(gt=0, strict=True)
    identity_hash: str = Field(pattern=HASH)
    project_scope: Literal['powersystem']
    criticality: Literal['core', 'support', 'drill']
    verified: bool = Field(strict=True)


class Fact(Strict):
    key: str
    value: StrictStr | StrictBool | StrictInt | StrictFloat | None

    @field_validator('key')
    @classmethod
    def key_allowed(cls, value):
        if value not in FACT_KEYS: raise ValueError('unlicensed_fact')
        return value

    @field_validator('value', mode='before')
    @classmethod
    def scalar(cls, value):
        if type(value) not in (str, bool, int, float, type(None)): raise ValueError('invalid_fact_value')
        if isinstance(value, str):
            if len(value) > 200: raise ValueError('fact_too_long')
            safe_text(value)
        if type(value) is float and not math.isfinite(value): raise ValueError('nonfinite_fact')
        return value


class Evidence(Strict):
    ref: str = Field(min_length=1, max_length=120, pattern=r'^[a-zA-Z0-9_.:/-]+$')
    source: Literal['program', 'synthetic']
    collected_at: datetime
    sampled_at: datetime | None = None
    complete: bool = Field(strict=True)
    filtered: bool = Field(strict=True)
    truncated: bool = Field(strict=True)
    facts: list[Fact] = Field(default_factory=list, max_length=20)
    # Only explicit synthetic samples may contain untrusted source text in M1.
    untrusted_excerpt: str | None = Field(default=None, max_length=500)

    @model_validator(mode='after')
    def boundaries(self):
        for dt in (self.collected_at, self.sampled_at):
            if dt is not None and dt.utcoffset() is None: raise ValueError('timezone_required')
        if self.untrusted_excerpt is not None:
            if self.source != 'synthetic': raise ValueError('raw_material_blocked')
            safe_text(self.untrusted_excerpt)
        if len({f.key for f in self.facts}) != len(self.facts): raise ValueError('duplicate_fact')
        return self


class Egress(Strict):
    destination: str = Field(min_length=1, max_length=80, pattern=r'^[a-zA-Z0-9_.-]+$')
    scope_version: Literal['sec-m1-synthetic/v3']
    fields: list[Literal['normalized_intent', 'tool', 'asset', 'evidence', 'policy_version']] = Field(min_length=1, max_length=5)
    payload_hash: str = Field(pattern=HASH)


class LocalEgress(Strict):
    destination: Literal['existing-gateway']
    scope_version: Literal['sec-m2-local-drill/v1']
    receiver: Literal['operation', 'supervisor']
    model: Literal['deepseek-v4-flash-0731', 'deepseek-v4.1-flash']
    fields: list[Literal['normalized_intent', 'tool', 'asset', 'evidence', 'policy_version']] = Field(min_length=1, max_length=5)
    payload_hash: str = Field(pattern=HASH)

    @model_validator(mode='after')
    def receiver_model(self):
        if self.model != ('deepseek-v4.1-flash' if self.receiver == 'supervisor' else 'deepseek-v4-flash-0731'):
            raise ValueError('unlicensed_egress_model')
        if len(self.fields)!=len(set(self.fields)): raise ValueError('duplicate_egress_field')
        return self


class InspectionEgress(LocalEgress):
    scope_version: Literal['sec-m3-local-inspection/v1']


class M4Egress(LocalEgress):
    scope_version: Literal['sec-m4-local-integration/v1', 'sec-m4-project-readonly/v1', 'sec-m4-local-integration/v2', 'sec-m4-project-readonly/v2', 'sec-m4-project-readonly/v3', 'sec-m4-project-readonly/v4', 'sec-m4-project-readonly/v5']


class ReviewRequest(Strict):
    protocol: Literal['ma-review/v1']
    review_id: UUID
    task_id: UUID
    trace_id: UUID
    step_no: int = Field(ge=1, strict=True)
    execution_epoch: int = Field(ge=0, strict=True)
    kind: KINDS
    actor: Actor
    tool: Tool
    asset: Asset | None = None
    policy_version: Literal['sec-m1-policy/v6', 'sec-m2-policy/v1', 'sec-m2-policy/v2', 'sec-m2-policy/v3', 'sec-m2-policy/v4', 'sec-m2-policy/v5', 'sec-m3-policy/v1', 'sec-m3-policy/v2', 'sec-m3-policy/v3', 'sec-m4-policy/v1', 'sec-m4-policy/v2', 'sec-m4-policy/v3', 'sec-m4-policy/v4', 'sec-m4-policy/v5']
    scope_version: Literal['sec-m1-synthetic/v3', 'sec-m2-local-drill/v1', 'sec-m3-local-inspection/v1', 'sec-m4-local-integration/v1', 'sec-m4-project-readonly/v1', 'sec-m4-local-integration/v2', 'sec-m4-project-readonly/v2', 'sec-m4-project-readonly/v3', 'sec-m4-project-readonly/v4', 'sec-m4-project-readonly/v5']
    normalized_intent: str = Field(min_length=1, max_length=1000)
    evidence: list[Evidence] = Field(max_length=20)
    egress: Egress | LocalEgress | InspectionEgress | M4Egress | None = None
    deadline: datetime

    @model_validator(mode='after')
    def boundaries(self):
        safe_text(self.normalized_intent)
        if self.deadline.utcoffset() is None: raise ValueError('timezone_required')
        if len({e.ref for e in self.evidence}) != len(self.evidence): raise ValueError('duplicate_evidence_ref')
        if self.kind == 'data_egress' and self.egress is None: raise ValueError('egress_required')
        if self.tool.risk_class == 'R2' and self.asset is None: raise ValueError('asset_required')
        if (self.policy_version, self.scope_version) not in ((POLICY, SCOPE), ('sec-m2-policy/v1', RUNTIME_SCOPE), ('sec-m2-policy/v2', RUNTIME_SCOPE), ('sec-m2-policy/v3', RUNTIME_SCOPE), ('sec-m2-policy/v4', RUNTIME_SCOPE), (RUNTIME_POLICY, RUNTIME_SCOPE), (INSPECTION_POLICY, INSPECTION_SCOPE), ('sec-m3-policy/v1',INSPECTION_SCOPE), ('sec-m3-policy/v2',INSPECTION_SCOPE), (M4_POLICY,M4_LOCAL_SCOPE), (M4_POLICY,M4_SERVER_SCOPE_PREVIOUS), (M4_SERVER_POLICY_V3,M4_SERVER_SCOPE_V3), (M4_SERVER_POLICY_V4,M4_SERVER_SCOPE_V4), (M4_SERVER_POLICY,M4_SERVER_SCOPE), ('sec-m4-policy/v1','sec-m4-local-integration/v1'), ('sec-m4-policy/v1','sec-m4-project-readonly/v1')):
            raise ValueError('policy_scope_mismatch')
        if self.scope_version == SCOPE:
            if any(f.key not in M1_FACT_KEYS for e in self.evidence for f in e.facts):
                raise ValueError('unlicensed_fact')
            if self.egress is not None and not isinstance(self.egress, Egress): raise ValueError('unlicensed_egress')
        else:
            if any(e.source != 'program' or e.untrusted_excerpt is not None for e in self.evidence):
                raise ValueError('runtime_program_evidence_required')
            if self.scope_version==INSPECTION_SCOPE:
                if self.tool.risk_class!='R0' or self.kind in ('write_plan','write_preflight','post_execution'): raise ValueError('inspection_read_only_required')
                if self.egress is not None and (not isinstance(self.egress, InspectionEgress) or self.egress.scope_version!=self.scope_version): raise ValueError('unlicensed_egress')
            elif self.scope_version in (M4_LOCAL_SCOPE,M4_SERVER_SCOPE_PREVIOUS,M4_SERVER_SCOPE_V3,M4_SERVER_SCOPE_V4,M4_SERVER_SCOPE,'sec-m4-local-integration/v1','sec-m4-project-readonly/v1'):
                if self.scope_version in (M4_SERVER_SCOPE_PREVIOUS,M4_SERVER_SCOPE_V3,M4_SERVER_SCOPE_V4,M4_SERVER_SCOPE,'sec-m4-project-readonly/v1') and (self.tool.risk_class!='R0' or self.kind in ('write_plan','write_preflight','post_execution')): raise ValueError('server_read_only_required')
                if self.egress is not None and (not isinstance(self.egress,M4Egress) or self.egress.scope_version!=self.scope_version): raise ValueError('unlicensed_egress')
            elif self.egress is not None and (not isinstance(self.egress, LocalEgress) or self.egress.scope_version!=self.scope_version): raise ValueError('unlicensed_egress')
        return self


class Opinion(Strict):
    """Model opinion only. Association identifiers are exclusively program-owned."""
    decision: Literal['allow', 'deny', 'needs_clarification']
    reason: str = Field(min_length=1, max_length=1200)
    rule_hits: list[str] = Field(min_length=1, max_length=12)
    evidence_refs: list[str] = Field(max_length=20)

    @model_validator(mode='after')
    def boundaries(self):
        safe_text(self.reason)
        if any(r not in RULES for r in self.rule_hits) or len(set(self.rule_hits)) != len(self.rule_hits):
            raise ValueError('invalid_rule_hits')
        if len(set(self.evidence_refs)) != len(self.evidence_refs): raise ValueError('duplicate_evidence_ref')
        return self


class Decision(Opinion):
    protocol: Literal['ma-review/v1']
    review_id: UUID


def validate_opinion(value, request, secrets=()):
    result=Opinion.model_validate(value)
    if not set(result.evidence_refs).issubset({e.ref for e in request.evidence}):raise ValueError('invalid_evidence_ref')
    safe_text(canonical(result.model_dump(mode='json')).decode(),secrets)
    return result


def validate_decision(value, request, secrets=()):
    result = Decision.model_validate(value)
    if result.review_id != request.review_id: raise ValueError('review_correlation_mismatch')
    if not set(result.evidence_refs).issubset({e.ref for e in request.evidence}): raise ValueError('invalid_evidence_ref')
    safe_text(canonical(result.model_dump(mode='json')).decode(), secrets)
    return result


def projection(request, secrets=()):
    raw = request.model_dump(mode='json')
    safe_text(canonical(raw).decode(), secrets)
    if request.asset and not request.asset.verified: raise ValueError('asset_not_verified')
    if request.tool.risk_class == 'R2':
        if request.actor.role != 'admin': raise ValueError('admin_required')
        if request.tool.name != 'docker.restart_drill_container' or request.asset.criticality != 'drill':
            raise ValueError('write_scope_blocked')
    allowed = {'protocol', 'kind', 'normalized_intent', 'tool', 'asset', 'policy_version', 'evidence', 'egress'}
    result = {k: raw[k] for k in allowed}
    result['actor_role'] = request.actor.role
    result['execution_epoch']=request.execution_epoch
    result['reviewed_at']=datetime.now(timezone.utc).isoformat()
    if request.scope_version in (RUNTIME_SCOPE,INSPECTION_SCOPE,M4_LOCAL_SCOPE,M4_SERVER_SCOPE_PREVIOUS,M4_SERVER_SCOPE_V3,M4_SERVER_SCOPE_V4,M4_SERVER_SCOPE): result['scope_version'] = request.scope_version
    if len(canonical(result)) > 32768: raise ValueError('projection_too_large')
    return result


def sign_message(key, audience, review_id, payload_hash, expires_at):
    now = int(time.time())
    claims = {'kid': 'sec-m1', 'aud': audience, 'review_id': str(review_id), 'hash': payload_hash,
              'iat': now, 'exp': int(expires_at)}
    payload = base64.urlsafe_b64encode(canonical(claims)).decode().rstrip('=')
    return payload + '.' + hmac.new(key.encode(), payload.encode(), hashlib.sha256).hexdigest()


def verify_message(token, key, audience, review_id, payload_hash):
    try:
        if len(key) < 32 or len(token) > 4096: raise ValueError()
        payload, signature = token.split('.')
        expected = hmac.new(key.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature): raise ValueError()
        claims = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
        if set(claims) != {'kid', 'aud', 'review_id', 'hash', 'iat', 'exp'}: raise ValueError()
        now = int(time.time())
        if claims['kid'] != 'sec-m1' or claims['aud'] != audience or claims['review_id'] != str(review_id) or claims['hash'] != payload_hash:
            raise ValueError()
        if type(claims['iat']) is not int or type(claims['exp']) is not int: raise ValueError()
        if claims['iat'] > now + 5 or claims['exp'] <= now or not 0 < claims['exp'] - claims['iat'] <= 300:
            raise ValueError()
        return claims
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise ValueError('invalid_review_signature') from None
