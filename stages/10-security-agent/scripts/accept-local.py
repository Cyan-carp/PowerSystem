"""Run approved synthetic real-model cases through the real program/store/service path."""
import argparse
import asyncio
import base64
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from datetime import datetime, timezone

import asyncpg
import httpx

ROOT=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(ROOT/'stages/10-security-agent'),str(ROOT/'stages/09-ops-agent')]
from ops_agent.reviews import ReviewController
from samples import CASES, install_base, seed, standalone_request
from review_contract import SCOPE, digest, sign_message, validate_decision, verify_message


def manifest():
    files=list((ROOT/'stages/10-security-agent').rglob('*'))+[ROOT/'stages/09-ops-agent/ops_agent/reviews.py',ROOT/'stages/09-ops-agent/deploy/005_security_reviews.sql']
    return {str(p.relative_to(ROOT)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(files) if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc'}


async def main(args):
    run=(ROOT/args.run).resolve();out=(ROOT/args.output).resolve()
    if not run.is_relative_to(ROOT/'artifacts/stage10-security-agent') or not out.is_relative_to(run):raise ValueError('invalid_sec_output')
    if args.service_only and getattr(args,'acceptance_timeout_exception',False):
        raise ValueError('timeout_exception_requires_program_store')
    out.mkdir(parents=True,exist_ok=False)
    frozen=manifest();(out/'source-manifest.json').write_text(json.dumps(frozen,indent=2),encoding='utf-8')
    private=run/'private';pool=None
    token,key,result_key=[(private/name).read_text().strip() for name in ('sec-service-token','sec-request-key','sec-result-key')]
    if not args.service_only:
        db=json.loads((private/'databases.json').read_text())
        if db['acceptance_database']!='sec_m1_acceptance_test' or db['port']!=15432:raise ValueError('isolated_acceptance_database_required')
        pool=await asyncpg.create_pool(host=db['host'],port=db['port'],user=db['user'],password=Path(db['password_file']).read_text().strip(),
                                     database=db['acceptance_database'],min_size=1,max_size=5)
    results=[];blocked=None
    try:
        if pool:await install_base(pool)
        async with httpx.AsyncClient(trust_env=False,follow_redirects=False) as client:
            timeout=getattr(args,'review_timeout',30)
            exception=getattr(args,'acceptance_timeout_exception',False)
            controller=ReviewController(pool,client,token,key,result_key,model=args.model,timeout=timeout,
                acceptance_timeout_exception=exception) if pool else None
            if controller:await controller.install()
            health=(await client.get('http://127.0.0.1:8100/health',timeout=3)).json()
            if not health['enabled'] or health['model']!=args.model:raise ValueError('unexpected_security_service')
            if health.get('model_timeout_seconds',30)!=timeout or health.get('acceptance_timeout_exception',False)!=exception:
                raise ValueError('unexpected_security_budget')
            for case in CASES:
                if controller:
                    params=await seed(pool,case)
                    request=await controller.build(**params)
                    row=await controller.submit(**params)
                    valid=await controller.valid(params['review_id'],params['user_id'],request)
                else:
                    request=standalone_request(case)
                    row,valid=await review_service(client,request,token,key,result_key)
                value=row['decision']['decision'] if row['decision'] else None
                record={'case_id':case['id'],'expected':case['expected'],'actual':value,'state':row['state'],
                        'review_id':str(request.review_id),'task_id':str(request.task_id), 'kind':case['kind'],
                        'error_code':row['error_code'],'usage':row['usage'],'duration_ms':row['duration_ms'],
                        'valid':valid,'database_verified':controller is not None,'validation_code':row.get('validation_code'),
                        'request':request.model_dump(mode='json'),'decision':row['decision']}
                record['passed']=row['state']=='completed' and value==case['expected'] and record['valid']
                results.append(record)
                (out/'cases.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
                if row['state'] in ('invalid','unavailable'):
                    blocked=row['error_code'];break
                # Verify reuse against the saved record, without a second model call.
                if controller:
                    replay=await controller.submit(**params)
                    if replay['decision_hash']!=row['decision_hash']:raise ValueError('replay_record_changed')
                print(json.dumps({'case_id':case['id'],'expected':case['expected'],'actual':value,'passed':record['passed']},ensure_ascii=False),flush=True)
    finally:
        if pool:await pool.close()
    summary={'model':args.model,'gateway':'https://hgapi.dieqiyun.top/v1','scope':SCOPE,
             'real_model':True,'planned_cases':len(CASES),'executed':len(results),'passed':sum(r['passed'] for r in results),
             'model_timeout_seconds':getattr(args,'review_timeout',30),
             'acceptance_timeout_exception':getattr(args,'acceptance_timeout_exception',False),
             'dangerous_allow':sum(r['actual']=='allow' and r['expected']=='deny' for r in results),
             'legal_false_deny':sum(r['actual']=='deny' and r['expected']=='allow' for r in results),
             'clarification_correct':sum(r['actual']=='needs_clarification' and r['expected']=='needs_clarification' for r in results),
             'usage_total':{k:sum(r['usage'].get(k,0) for r in results) for k in ('prompt_tokens','completion_tokens','total_tokens')},
             'database_verified':not args.service_only,'layer':'service-model' if args.service_only else 'program-store-service-model',
             'blocked':blocked,'source_unchanged':manifest()==frozen,'timestamp':datetime.now(timezone.utc).isoformat()}
    summary['sample_suite_passed']=not blocked and len(results)==len(CASES) and all(r['passed'] for r in results) and summary['source_unchanged']
    summary['signed_off']=summary['sample_suite_passed'] and summary['database_verified']
    summary['standard_30s_accepted']=summary['sample_suite_passed'] and not summary['acceptance_timeout_exception']
    (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False))
    return 0 if summary['sample_suite_passed'] else 1


async def review_service(client,request,token,key,result_key):
    """Signed HTTP check only. Never manufactures a persisted review record."""
    raw=request.model_dump(mode='json');start=time.monotonic()
    row={'state':'unavailable','decision':None,'error_code':None,'usage':{},'duration_ms':None}
    try:
        response=await client.post('http://127.0.0.1:8100/internal/ma-review/v1/reviews',json=raw,headers={
            'X-SEC-Service-Token':token,'X-SEC-Request':sign_message(key,'ma-review-request/v1',request.review_id,
            digest(raw),min(int(request.deadline.timestamp()),int(time.time())+300))},timeout=35)
        row['duration_ms']=int((time.monotonic()-start)*1000)
        if response.status_code!=200:
            code=response.json().get('detail','security_unavailable')
            if not isinstance(code,str) or len(code)>80:code='security_unavailable'
            row.update(state='invalid' if response.status_code in (400,401,403,422) else 'unavailable',error_code=code)
            row['validation_code']=response.headers.get('X-SEC-Validation')
            return row,False
        result=validate_decision(response.json(),request,(token,key,result_key))
        metadata=json.loads(base64.urlsafe_b64decode(response.headers.get('X-SEC-Metadata','')))
        if set(metadata)!={'request_hash','decision_hash','usage','duration_ms'}:raise ValueError('invalid_metadata')
        if metadata['request_hash']!=digest(raw) or metadata['decision_hash']!=digest(result.model_dump(mode='json')):raise ValueError('result_binding_mismatch')
        claims=verify_message(response.headers.get('X-SEC-Result',''),result_key,'ma-review-result/v1',request.review_id,digest(metadata))
        if claims['exp']>int(request.deadline.timestamp()):raise ValueError('result_expiry_invalid')
        usage=metadata['usage']
        if not isinstance(usage,dict) or any(k not in ('prompt_tokens','completion_tokens','total_tokens') or type(v) is not int or v<0 for k,v in usage.items()):raise ValueError('invalid_usage')
        row.update(state='completed',decision=result.model_dump(mode='json'),usage=usage)
        return row,True
    except httpx.HTTPError:
        row['error_code']='security_unavailable';return row,False
    except (ValueError,KeyError,TypeError):
        row.update(state='invalid',error_code='security_invalid');return row,False


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--run',required=True);parser.add_argument('--output',required=True)
    parser.add_argument('--model',default='deepseek-v4-flash-0731')
    parser.add_argument('--review-timeout',type=float,default=30)
    parser.add_argument('--acceptance-timeout-exception',action='store_true')
    parser.add_argument('--service-only',action='store_true',help='Explicit service/model sample suite; no database or milestone sign-off')
    sys.exit(asyncio.run(main(parser.parse_args())))
