"""Strict, independent MA-M3 authorization. No executable text or caller Docker options."""
import hashlib
import hmac
import base64
import json
import re
import time
from uuid import UUID
from ops_contract import canonical, digest, asset_id

TOOL = 'docker.restart_drill_container'
FIELDS = {'aud','kid','iat','exp','action','task_id','trace_id','execution_epoch','step_no','call_id',
          'user_id','role','asset_id','asset_version','registry_hash','args_hash','deadline',
          'approval_id','container_id','image_digest','config_hash','policy_version'}
PROOF_FIELDS = {'asset_id','asset_version','registry_hash','container_id','image_digest','config_hash',
                'policy_version','state','running','health','started_at','stateless_verified','collected_at'}


def restart_args(value):
    if not isinstance(value,dict) or set(value)!={'asset_id'}: raise ValueError('invalid_args')
    return {'asset_id':asset_id(value['asset_id'])}


def sign(value,key):
    if len(key)<32: raise ValueError('write_credentials_required')
    payload=base64.urlsafe_b64encode(canonical(value)).decode().rstrip('=')
    return payload+'.'+hmac.new(key.encode(),payload.encode(),hashlib.sha256).hexdigest()


def verify(token,key):
    try:
        if not isinstance(token,str) or len(token)>12000 or len(key)<32: raise ValueError()
        payload,mac=token.split('.')
        if not hmac.compare_digest(hmac.new(key.encode(),payload.encode(),hashlib.sha256).hexdigest(),mac): raise ValueError()
        v=json.loads(base64.urlsafe_b64decode(payload+'='*(-len(payload)%4)))
        if set(v)!=FIELDS or v['aud']!='ma-drill-executor' or v['kid']!='ma-write-v1' or v['action'] not in ('preflight','restart'): raise ValueError()
        now=time.time()
        for k in ('iat','exp','execution_epoch','step_no','user_id','asset_version'):
            if type(v[k]) is not int or v[k]<1: raise ValueError()
        if v['iat']>now+1 or v['exp']<=now or not 0<v['exp']-v['iat']<=30: raise ValueError()
        if type(v['deadline']) not in (int,float) or not __import__('math').isfinite(v['deadline']) or v['deadline']<=now or v['role']!='admin': raise ValueError()
        for k in ('task_id','trace_id','call_id'): UUID(v[k])
        asset_id(v['asset_id'])
        for k in ('registry_hash','args_hash'):
            if not re.fullmatch('[a-f0-9]{64}',v[k]): raise ValueError()
        if v['action']=='restart':
            UUID(v['approval_id'])
            if not re.fullmatch('[a-f0-9]{64}',v['container_id']) or not re.fullmatch('sha256:[a-f0-9]{64}',v['image_digest']): raise ValueError()
            for k in ('config_hash','policy_version'):
                if not re.fullmatch('[a-f0-9]{64}',v[k]): raise ValueError()
        elif any(v[k] is not None for k in ('approval_id','container_id','image_digest','config_hash','policy_version')): raise ValueError()
        return v
    except (ValueError,TypeError,KeyError,AttributeError): raise ValueError('invalid_write_grant') from None


def proof(value):
    from datetime import datetime
    if not isinstance(value,dict) or set(value)!=PROOF_FIELDS: raise ValueError('invalid_result')
    asset_id(value['asset_id'])
    for k in ('registry_hash','config_hash','policy_version','container_id'):
        if not isinstance(value[k],str) or not re.fullmatch('[a-f0-9]{64}',value[k]): raise ValueError('invalid_result')
    if type(value['asset_version']) is not int or value['asset_version']<1 or not re.fullmatch('sha256:[a-f0-9]{64}',value['image_digest']): raise ValueError('invalid_result')
    if type(value['running']) is not bool or value['stateless_verified'] is not True or value['state'] not in ('running','exited','restarting','created','dead'): raise ValueError('invalid_result')
    if value['health'] not in ('healthy','unhealthy','starting','not_configured'): raise ValueError('invalid_result')
    for k in ('started_at','collected_at'):
        if datetime.fromisoformat(value[k].replace('Z','+00:00')).tzinfo is None: raise ValueError('invalid_result')
    return value


def matches(snapshot,current):
    return all(snapshot[k]==current[k] for k in ('asset_id','asset_version','registry_hash','container_id','image_digest','config_hash','policy_version'))


def explicit_restart(text):
    """Fail closed for explanatory/conditional requests; approval remains mandatory."""
    if not isinstance(text,str): return False
    if re.search(r'不要|禁止|别重启|不重启|如何|怎么|解释|如果|假如|how\s+(?:to|do)|do\s+not|don.t',text,re.I): return False
    return bool(re.search(r'重启|\brestart\b',text,re.I))


def snapshot_grant(aid,key):
    now=int(time.time())
    return sign({'aud':'ma-drill-snapshot','asset_id':asset_id(aid),'iat':now,'exp':now+30},key)


def verify_snapshot(token,key):
    try:
        payload,mac=token.split('.')
        if not hmac.compare_digest(hmac.new(key.encode(),payload.encode(),hashlib.sha256).hexdigest(),mac): raise ValueError()
        v=json.loads(base64.urlsafe_b64decode(payload+'='*(-len(payload)%4)))
        if set(v)!={'aud','asset_id','iat','exp'} or v['aud']!='ma-drill-snapshot': raise ValueError()
        if type(v['iat']) is not int or type(v['exp']) is not int or v['iat']>time.time()+1 or v['exp']<=time.time() or not 0<v['exp']-v['iat']<=30: raise ValueError()
        return asset_id(v['asset_id'])
    except (ValueError,TypeError,KeyError): raise ValueError('invalid_write_grant') from None
