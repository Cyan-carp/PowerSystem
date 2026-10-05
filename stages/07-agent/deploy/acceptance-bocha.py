"""Authorized read-only five-question acceptance through the existing JWT proxy."""
import argparse
import base64
import hashlib
import hmac
import json
import subprocess
import time
import urllib.request
from pathlib import Path
from source_review import review_items, protocol_passes

QUESTIONS = ["风机覆冰应该怎么排查？", "逆变器电弧故障如何排查？", "逆变器接地与绝缘异常如何排查？", "光伏电弧问题怎么排查？", "风机过热故障怎么排查？"]


def main(output):
    root = Path('/opt/powersystem'); output.mkdir(parents=True,exist_ok=True)
    private = dict(line.split('=',1) for line in (root/'.env').read_text().splitlines() if '=' in line and not line.startswith('#'))
    user = subprocess.check_output(['docker','exec','powersystem-stage4-postgres-1','psql','-U','powersystem','-d','powersystem','-Atqc',"SELECT id FROM users WHERE role='operator' ORDER BY id LIMIT 1"],text=True).strip().splitlines()[0]
    encode=lambda value:base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip('=')
    data=encode({'alg':'HS256','typ':'JWT'})+'.'+encode({'sub':user,'exp':int(time.time())+1800})
    token=data+'.'+base64.urlsafe_b64encode(hmac.new(private['JWT_SECRET'].encode(),data.encode(),hashlib.sha256).digest()).decode().rstrip('=')
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def chat(question):
        request=urllib.request.Request('https://8.138.10.222/api/v1/agent/chat',data=json.dumps({'message':question}).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+token})
        with opener.open(request,timeout=50) as result:return json.load(result)['data']
    records=[]; pending_review=[]
    for index,question in enumerate(QUESTIONS):
        started=time.monotonic()
        try:
            response=chat(question)
            available={e['id'] for e in response['evidence'] if e['status']=='ok' and e['kind']=='web'}
            claims=([response['conclusion']] if response['conclusion'] else [])+response['suggestions']
            valid=all(claim['evidence_ids'] and set(claim['evidence_ids'])<=available for claim in claims)
            protocol_passed=valid and protocol_passes(response)
            if protocol_passed:
                pending_review.extend(review_items(index,response))
            record={'question':question,'response':response,'protocol_passed':protocol_passed,
                    'source_review':'pending' if protocol_passed else 'not_applicable', 'passed':False,
                    'elapsed_seconds':round(time.monotonic()-started,3)}
        except Exception as error:
            record={'question':question,'passed':False,'error':type(error).__name__}
        folder=output/('protocol-success' if record.get('protocol_passed') else 'failure');folder.mkdir(exist_ok=True)
        (folder/f'answer-{index}.json').write_text(json.dumps(record,ensure_ascii=False,indent=2))
        summary={k:v for k,v in record.items() if k!='response'};records.append(summary)
        (output/'summary.json').write_text(json.dumps(records,ensure_ascii=False,indent=2))
        print(json.dumps(summary,ensure_ascii=False),flush=True)
    # Local hit must not search; private state cannot be replaced by public evidence.
    for name,question in [('local-hit','告警确认怎么操作？'),('private-state','我的设备风机覆冰怎么排查？')]:
        response=chat(question)
        (output/(name+'.json')).write_text(json.dumps(response,ensure_ascii=False,indent=2))
        assert response['web_status'] in ('not_requested','not_eligible')
        assert not any(e['kind']=='web' for e in response['evidence'])
    (output/'source-review-template.json').write_text(json.dumps(pending_review,ensure_ascii=False,indent=2))
    # This command checks protocol only; the separate finalizer records review policy.
    return all(record.get('protocol_passed') for record in records)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    raise SystemExit(0 if main(parser.parse_args().output) else 1)
