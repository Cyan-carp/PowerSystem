"""One explicitly synthetic service/model availability probe; not database acceptance."""
import argparse
import asyncio
import base64
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from uuid import uuid4

import httpx
ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'stages/10-security-agent'))
from review_contract import POLICY,SCOPE,ReviewRequest,digest,sign_message,validate_decision,verify_message
from samples import CASES,context


async def main(args):
    run=(ROOT/args.run).resolve();out=(ROOT/args.output).resolve()
    if not run.is_relative_to(ROOT/'artifacts/stage10-security-agent') or not out.is_relative_to(run):raise ValueError('invalid_sec_output')
    out.mkdir(parents=True,exist_ok=False);private=run/'private'
    token,key,result_key=[(private/name).read_text().strip() for name in ('sec-service-token','sec-request-key','sec-result-key')]
    request=ReviewRequest(protocol='ma-review/v1',review_id=uuid4(),task_id=uuid4(),trace_id=uuid4(),step_no=1,execution_epoch=1,
        kind='sensitive_read',actor={'user_id':100101,'role':'operator'},policy_version=POLICY,scope_version=SCOPE,
        normalized_intent=CASES[0]['intent'],tool={'name':'linux.get_cpu_usage','version':'1.0','risk_class':'R0',
        'args_hash':digest({'synthetic':True}),'plan_hash':digest({'synthetic':'probe'})},evidence=context(CASES[0])['evidence'],
        deadline=datetime.now(timezone.utc)+timedelta(seconds=60))
    raw=request.model_dump(mode='json');start=time.monotonic()
    async with httpx.AsyncClient(trust_env=False,follow_redirects=False,timeout=35) as client:
        health=(await client.get('http://127.0.0.1:8100/health')).json()
        if not health.get('enabled') or health.get('model')!=args.model:raise ValueError('unexpected_security_service')
        response=await client.post('http://127.0.0.1:8100/internal/ma-review/v1/reviews',json=raw,headers={
            'X-SEC-Service-Token':token,'X-SEC-Request':sign_message(key,'ma-review-request/v1',request.review_id,digest(raw),int(request.deadline.timestamp()))})
    summary={'model':args.model,'scope':SCOPE,'real_model':True,'database_verified':False,
             'status_code':response.status_code,'duration_ms':int((time.monotonic()-start)*1000),'available':False}
    if response.status_code==200:
        decision=validate_decision(response.json(),request,(token,key,result_key))
        meta=json.loads(base64.urlsafe_b64decode(response.headers['X-SEC-Metadata']))
        verify_message(response.headers['X-SEC-Result'],result_key,'ma-review-result/v1',request.review_id,digest(meta))
        if meta['request_hash']!=digest(raw) or meta['decision_hash']!=digest(decision.model_dump(mode='json')):raise ValueError('probe_binding_mismatch')
        summary.update(available=True,decision=decision.model_dump(mode='json'),usage=meta['usage'])
    else:summary['error_code']=response.json().get('detail','security_unavailable')
    (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False))
    return 0 if summary['available'] else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--run',required=True);parser.add_argument('--output',required=True)
    parser.add_argument('--model',default='deepseek-v4-flash-0731')
    sys.exit(asyncio.run(main(parser.parse_args())))
