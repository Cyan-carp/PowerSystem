"""S06 typed action domain. No URLs, commands or caller-selected risk labels.

Canonicalization is RFC 8785 over this restricted domain: strings, booleans,
null, arrays and safe integers. Floating point values are not action arguments.
"""
import base64
import hashlib
import hmac
import json
import time
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator
from uuid import UUID

POLICY='v4-s06-local/v1'
CAPABILITIES={'alarm.ack':('R1','internal'), 'device.remark':('R1','internal'),
              'device.remark.read':('R0','sensitive')}

def canonical(value):
    def check(v):
        if v is None or type(v) is bool:return v
        if type(v) is int and abs(v)<=9007199254740991:return v
        if isinstance(v,str):v.encode('utf-8');return v
        if isinstance(v,list):return [check(x) for x in v]
        if isinstance(v,dict) and all(isinstance(k,str) for k in v):
            return {k:check(v[k]) for k in sorted(v,key=lambda k:k.encode('utf-16-be'))}
        raise ValueError('non_canonical_action_value')
    return json.dumps(check(value),ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()

def digest(value):return hashlib.sha256(canonical(value)).hexdigest()

class Strict(BaseModel):model_config=ConfigDict(extra='forbid')
class Ack(Strict):alarm_id:int=Field(gt=0,le=9007199254740991,strict=True)
class Remark(Strict):
    device_id:int=Field(gt=0,le=9007199254740991,strict=True)
    remark:str=Field(max_length=1000,strict=True)
    expected_version:int=Field(ge=0,le=9007199254740991,strict=True)
    @field_validator('remark')
    @classmethod
    def plain(cls,v):
        if any(ord(c)<32 and c not in '\n\t\r' for c in v):raise ValueError('invalid_plain_text')
        v.encode('utf-8');return v
class Read(Strict):device_id:int=Field(gt=0,le=9007199254740991,strict=True)
class Proposal(Strict):
    action_id:UUID
    run_id:UUID
    session_id:UUID
    parent_turn_id:int=Field(gt=0,strict=True)
    capability:Literal['alarm.ack','device.remark','device.remark.read']
    typed_args:dict
    user_request:str=Field(min_length=1,max_length=32000)
class Decision(Strict):
    run_id:UUID
    approval_id:UUID
    plan_hash:str=Field(pattern='^[a-f0-9]{64}$')
    decision:Literal['approve','reject']

def arguments(capability,value):
    return {'alarm.ack':Ack,'device.remark':Remark,'device.remark.read':Read}[capability].model_validate(value).model_dump()

def binding(owner,proposal,args,version,expiry):
    risk,data=CAPABILITIES[proposal.capability]
    target=args.get('alarm_id',args.get('device_id'))
    return dict(owner_id=owner,run_id=str(proposal.run_id),action_id=str(proposal.action_id),
        capability=proposal.capability,version='1.0',typed_args=args,
        target_refs=[dict(kind='alarm' if proposal.capability=='alarm.ack' else 'device',id=target)],
        target_version=version,data_class=data,destination_id='platform_client',destination_version='1',
        policy_version=POLICY,expires_at=expiry)

def sign_grant(value,key):
    encoded=base64.urlsafe_b64encode(canonical(value)).decode().rstrip('=')
    sig=base64.urlsafe_b64encode(hmac.new(key.encode(),encoded.encode(),hashlib.sha256).digest()).decode().rstrip('=')
    return encoded+'.'+sig

def verify_grant(token,key):
    try:
        if len(key)<32 or len(token)>24000:raise ValueError()
        raw,sig=token.split('.')
        value=json.loads(base64.urlsafe_b64decode(raw+'='*(-len(raw)%4)))
        if not hmac.compare_digest(token,sign_grant(value,key)):raise ValueError()
        if value['aud']!='ma-business-execute/v1' or type(value['exp']) is not int or value['exp']<=time.time() or value['iat']>time.time()+5 or not 0<value['exp']-value['iat']<=300:raise ValueError()
        return value
    except (ValueError,KeyError,TypeError):raise ValueError('invalid_business_grant') from None
