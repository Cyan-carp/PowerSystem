"""Opt-in SEC-M1 program control. Never imported by the running Worker.

Only this controller owns the database connection. The Security service receives
the strict, projected request and cannot create tasks or execute any operation.
"""
import base64
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import httpx

from review_contract import (Asset, Decision, ReviewRequest, POLICY, SCOPE, digest, projection,
                             safe_text, sign_message, validate_decision, verify_message)


class ControlFailure(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class ReviewController:
    def __init__(self, pool, client, service_token, request_key, result_key,
                 url='http://127.0.0.1:8100', model='deepseek-v4-flash-0731', timeout=30, ttl=300,
                 acceptance_timeout_exception=False):
        if url not in ('http://127.0.0.1:8100', 'http://security-agent:8100', 'http://127.0.0.1:8110', 'http://127.0.0.1:8114'):
            raise ValueError('invalid_security_origin')
        if min(map(len, (service_token, request_key, result_key))) < 32 or len({service_token, request_key, result_key}) != 3:
            raise ValueError('security_private_credentials_required')
        if type(acceptance_timeout_exception) is not bool or (acceptance_timeout_exception and model != 'gpt-5.6-sol'):
            raise ValueError('invalid_security_timeout_exception')
        limit = 120 if acceptance_timeout_exception else 30
        if not 0 < timeout <= limit or not 1 <= ttl <= 300: raise ValueError('invalid_security_budget')
        self.pool, self.client = pool, client
        self.service_token, self.request_key, self.result_key = service_token, request_key, result_key
        self.url, self.model, self.timeout, self.ttl = url, model, timeout, ttl
        self.database_checked = False

    async def check_database(self):
        if not self.database_checked:
            name = await self.pool.fetchval('SELECT current_database()')
            if name not in ('sec_m1_acceptance_test', 'sec_m1_regression_test'):
                raise ControlFailure('isolated_sec_database_required')
            self.database_checked = True

    async def install(self):
        await self.check_database()
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute('SELECT pg_advisory_xact_lock(100100)')
            await conn.execute((Path(__file__).resolve().parents[1] / 'deploy/005_security_reviews.sql').read_text(encoding='utf-8'))

    async def recover(self):
        await self.check_database()
        # Explicit startup operation under a controller singleton lock.
        async with self.pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch("UPDATE ma_security_reviews SET state='unavailable',error_code='review_interrupted',finished_at=now() WHERE state='pending' RETURNING review_id")
            for row in rows:
                await conn.execute("INSERT INTO ma_security_review_events(review_id,state,reason) VALUES($1,'unavailable','review_interrupted')", row['review_id'])
        return len(rows)

    async def build(self, task_id, step_no, user_id, role, review_id, kind):
        """Read immutable program state; no caller-supplied plan, hashes or evidence."""
        await self.check_database()
        task = await self.pool.fetchrow('SELECT * FROM ma_tasks WHERE task_id=$1 AND user_id=$2', task_id, user_id)
        if task is None: raise ControlFailure('review_not_found')
        if role not in ('operator', 'admin'): raise ControlFailure('invalid_review_actor')
        if task['status'] not in ('planning', 'executing'): raise ControlFailure('review_task_inactive')
        step = await self.pool.fetchrow('SELECT * FROM ma_task_steps WHERE task_id=$1 AND step_no=$2', task_id, step_no)
        if step is None or step['execution_epoch'] != task['execution_epoch']: raise ControlFailure('review_context_mismatch')
        envelope = json.loads(task['envelope'])
        if envelope.get('actor') != {'user_id': user_id, 'role': role}:
            raise ControlFailure('invalid_review_actor')
        # SEC-M1 only runs explicitly synthetic tasks in isolated acceptance databases.
        if not envelope.get('intent', '').startswith('SEC-M1 synthetic:'):
            raise ControlFailure('synthetic_review_scope_required')
        args = json.loads(step['args'])
        if digest(args) != step['args_hash']: raise ControlFailure('review_args_mismatch')
        from .executor import MANIFEST
        from ops_contract import TOOLS, validate_args
        from ops_write_contract import restart_args
        if step['tool'] == 'docker.restart_drill_container':
            if step['version'] != '1.0' or step['risk_class'] != 'R2' or role != 'admin':
                raise ControlFailure('invalid_review_tool')
            restart_args(args)
        else:
            entry = MANIFEST.get(step['tool'])
            if not entry or not entry['enabled'] or step['version'] != entry['version'] or step['risk_class'] != entry['risk_class']:
                raise ControlFailure('invalid_review_tool')
            if step['tool'] in TOOLS: validate_args(step['tool'], args)
        result = json.loads(step['result']) if isinstance(step['result'], str) else step['result'] or {}
        context = result.get('security_context', {})
        if not isinstance(context, dict) or set(context) - {'evidence', 'egress'}:
            raise ControlFailure('security_projection_blocked')
        # Strict validation below rejects all unknown material, rather than passing arbitrary dicts.
        evidence = context.get('evidence', [])
        asset = None
        if step['asset_id']:
            current = await self.pool.fetchrow('SELECT * FROM ma_assets WHERE asset_id=$1', step['asset_id'])
            if current is None or not current['enabled'] or current['version'] != step['asset_version'] or not current['verified_at']:
                raise ControlFailure('review_asset_changed')
            snapshot = await self.pool.fetchval('SELECT snapshot FROM ma_asset_versions WHERE asset_id=$1 AND version=$2', step['asset_id'], step['asset_version'])
            if snapshot is None: raise ControlFailure('review_asset_missing')
            snapshot = json.loads(snapshot)
            if snapshot['identity_hash'] != current['identity_hash'] or args.get('asset_id') != step['asset_id']:
                raise ControlFailure('review_asset_changed')
            asset = {'asset_id': snapshot['asset_id'], 'version': snapshot['version'],
                     'identity_hash': snapshot['identity_hash'], 'project_scope': snapshot['project_scope'],
                     'criticality': snapshot['criticality'], 'verified': bool(snapshot.get('verified_at'))}
        plan = {'tool': step['tool'], 'version': step['version'], 'args': args, 'asset': asset}
        request = ReviewRequest(protocol='ma-review/v1', review_id=review_id, task_id=task_id,
            trace_id=task['trace_id'], step_no=step_no, execution_epoch=task['execution_epoch'], kind=kind,
            actor={'user_id': user_id, 'role': role}, tool={'name': step['tool'], 'version': step['version'],
                'risk_class': step['risk_class'], 'args_hash': step['args_hash'], 'plan_hash': digest(plan)},
            asset=asset, policy_version=POLICY, scope_version=SCOPE,
            normalized_intent=envelope['intent'].removeprefix('SEC-M1 synthetic:'), evidence=evidence,
            egress=context.get('egress'), deadline=task['deadline'])
        projection(request, (self.service_token, self.request_key, self.result_key))
        return request

    async def get(self, review_id, user_id):
        row = await self.pool.fetchrow('SELECT * FROM ma_security_reviews WHERE review_id=$1 AND user_id=$2', review_id, user_id)
        if row is None: raise ControlFailure('review_not_found')
        result = dict(row)
        for key in ('request', 'decision', 'usage'):
            if isinstance(result[key], str): result[key] = json.loads(result[key])
        if result['error_code']=='model_invalid_decision':
            reason=await self.pool.fetchval('SELECT reason FROM ma_security_review_events WHERE review_id=$1 ORDER BY event_seq DESC LIMIT 1',review_id)
            result['validation_code']=reason.split(':',1)[1] if reason and ':' in reason else None
        return result

    async def reserve(self, request):
        raw = request.model_dump(mode='json'); request_hash = digest(raw)
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute('SELECT pg_advisory_xact_lock(100101)')
            existing = await conn.fetchrow('SELECT request_hash,user_id FROM ma_security_reviews WHERE review_id=$1', request.review_id)
            if existing:
                if existing['user_id'] != request.actor.user_id: raise ControlFailure('review_not_found')
                if existing['request_hash'] != request_hash: raise ControlFailure('review_idempotency_conflict')
                return False
            if request.deadline.timestamp() <= time.time(): raise ControlFailure('security_review_stale')
            await conn.execute("INSERT INTO ma_security_reviews(review_id,task_id,step_no,trace_id,user_id,execution_epoch,kind,request_hash,binding_hash,request,state,model) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$8,$9::jsonb,'pending',$10)",
                request.review_id, request.task_id, request.step_no, request.trace_id, request.actor.user_id,
                request.execution_epoch, request.kind, request_hash, json.dumps(raw), self.model)
            await conn.execute("INSERT INTO ma_security_review_events(review_id,state,reason) VALUES($1,'pending','review_started')", request.review_id)
        return True

    async def finish(self, request, state, decision=None, metadata=None, expiry=None, error=None, validation_code=None):
        categories={'invalid_rule_hits','invalid_evidence_ref','review_correlation_mismatch',
                    'sensitive_material_blocked','duplicate_evidence_ref','decision_schema_invalid'}
        if validation_code not in categories:validation_code=None
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow('SELECT state FROM ma_security_reviews WHERE review_id=$1 FOR UPDATE', request.review_id)
            if row is None or row['state'] != 'pending': raise ControlFailure('review_completion_conflict')
            raw = decision.model_dump(mode='json') if decision else None
            await conn.execute('UPDATE ma_security_reviews SET state=$2,decision=$3::jsonb,decision_hash=$4,signature_validated=$5,expires_at=$6,error_code=$7,usage=$8::jsonb,duration_ms=$9,finished_at=now() WHERE review_id=$1',
                request.review_id, state, json.dumps(raw) if raw else None, digest(raw) if raw else None,
                state == 'completed', expiry, error, json.dumps((metadata or {}).get('usage', {})), (metadata or {}).get('duration_ms'))
            await conn.execute('INSERT INTO ma_security_review_events(review_id,state,reason) VALUES($1,$2,$3)',
                               request.review_id, state, (error+':'+validation_code) if error and validation_code else error or 'review_completed')

    async def submit(self, task_id, step_no, user_id, role, review_id, kind):
        request = await self.build(task_id, step_no, user_id, role, review_id, kind)
        if not await self.reserve(request): return await self.get(review_id, user_id)
        raw = request.model_dump(mode='json')
        started = time.monotonic()
        try:
            expiry = min(int(request.deadline.timestamp()), int(time.time()) + self.ttl)
            response = await self.client.post(self.url + '/internal/ma-review/v1/reviews', json=raw,
                headers={'X-SEC-Service-Token': self.service_token, 'X-SEC-Request': sign_message(self.request_key,
                    'ma-review-request/v1', request.review_id, digest(raw), expiry)},
                timeout=min(self.timeout, max(.001, request.deadline.timestamp() - time.time())))
            if response.status_code != 200:
                known = {'security_disabled', 'security_capacity', 'security_review_in_progress', 'security_review_stale',
                         'model_timeout', 'model_transport_unavailable', 'model_invalid_output', 'model_invalid_decision',
                         'security_invalid', 'security_projection_blocked', 'invalid_review_signature', 'security_unavailable', 'security_review_replayed'}
                try: code = response.json().get('detail')
                except ValueError: code = None
                if code not in known and not (isinstance(code, str) and code.startswith('model_http_') and code[11:].isdigit()):
                    code = 'security_unavailable'
                state = 'invalid' if response.status_code in (400, 401, 403, 422) else 'unavailable'
                category=response.headers.get('X-SEC-Validation') if code=='model_invalid_decision' else None
                await self.finish(request, state, metadata={'duration_ms': int((time.monotonic()-started)*1000)}, error=code,validation_code=category)
            else:
                try:
                    result = validate_decision(response.json(), request, (self.service_token, self.request_key, self.result_key))
                    metadata = json.loads(base64.urlsafe_b64decode(response.headers.get('X-SEC-Metadata', '')))
                    if set(metadata) != {'request_hash', 'decision_hash', 'usage', 'duration_ms'}:
                        raise ValueError('invalid_metadata')
                    if metadata['request_hash'] != digest(raw) or metadata['decision_hash'] != digest(result.model_dump(mode='json')):
                        raise ValueError('result_binding_mismatch')
                    claims = verify_message(response.headers.get('X-SEC-Result', ''), self.result_key,
                                            'ma-review-result/v1', request.review_id, digest(metadata))
                    if not isinstance(metadata['usage'], dict) or type(metadata['duration_ms']) is not int or metadata['duration_ms'] < 0:
                        raise ValueError('invalid_metadata')
                    if any(k not in ('prompt_tokens','completion_tokens','total_tokens') or type(v) is not int or v < 0 for k,v in metadata['usage'].items()):
                        raise ValueError('invalid_usage')
                    expiry = datetime.fromtimestamp(min(claims['exp'], int(time.time()) + self.ttl, request.deadline.timestamp()), timezone.utc)
                except (ValueError, TypeError, KeyError):
                    await self.finish(request, 'invalid', error='security_invalid')
                else:
                    try:
                        current = await self.build(task_id, step_no, user_id, role, review_id, kind)
                        if digest(current.model_dump(mode='json')) != digest(raw): raise ControlFailure('security_review_stale')
                    except ControlFailure:
                        await self.finish(request, 'invalid', error='security_review_stale')
                    else:
                        await self.finish(request, 'completed', result, metadata, expiry)
        except httpx.TimeoutException:
            await self.finish(request, 'unavailable', metadata={'duration_ms': int((time.monotonic()-started)*1000)}, error='security_timeout')
        except httpx.HTTPError:
            await self.finish(request, 'unavailable', metadata={'duration_ms': int((time.monotonic()-started)*1000)}, error='security_transport_unavailable')
        # A failed transaction deliberately propagates: no valid record is returned.
        return await self.get(review_id, user_id)

    async def valid(self, review_id, user_id, request):
        row = await self.get(review_id, user_id)
        if row['state'] != 'completed' or not row['signature_validated']: return False
        if row['binding_hash'] != digest(request.model_dump(mode='json')): return False
        if not row['expires_at'] or row['expires_at'] <= datetime.now(timezone.utc): return False
        if digest(row['decision']) != row['decision_hash']: return False
        try:
            validate_decision(row['decision'], request)
            current = await self.build(request.task_id, request.step_no, user_id, request.actor.role, review_id, request.kind)
            if digest(current.model_dump(mode='json')) != row['binding_hash']: return False
        except (ValueError, ControlFailure): return False
        return True
