"""Read-only deployed API checks with short-lived tokens; never print secrets."""
import argparse
import base64
import hashlib
import hmac
import json
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path


def mixed_answer_checks(response):
    if response['status'] != 'answered' or not response.get('conclusion'):
        return False, False, False
    valid_evidence = {e['id']: e for e in response['evidence'] if e['status'] == 'ok'}
    conclusion = set(response['conclusion']['evidence_ids'])
    all_refs = conclusion | {ref for suggestion in response['suggestions'] for ref in suggestion['evidence_ids']}
    return (True,
            any(valid_evidence[ref]['kind'] == 'business' and valid_evidence[ref]['tool'] == 'get_prediction'
                for ref in conclusion if ref in valid_evidence),
            any(valid_evidence[ref]['kind'] == 'document' for ref in all_refs if ref in valid_evidence))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, default=Path('/opt/powersystem'))
    parser.add_argument("--url", default='https://8.138.10.222')
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fault-check", action='store_true')
    parser.add_argument("--search-configured", action='store_true')
    parser.add_argument("--case", type=int, choices=range(7), help='run one numbered question for diagnosis')
    args = parser.parse_args()
    private = dict(line.split('=',1) for line in (args.project/'.env').read_text().splitlines() if '=' in line and not line.startswith('#'))
    def sql(query):
        result = subprocess.run(['docker','exec','powersystem-stage4-postgres-1','psql','-U','powersystem','-d','powersystem','-Atqc',query],text=True,capture_output=True,check=True)
        return result.stdout.strip().splitlines()[0]
    user = int(sql("SELECT id FROM users WHERE role='operator' ORDER BY id LIMIT 1"))
    encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip('=')
    data = encode({'alg':'HS256','typ':'JWT'})+'.'+encode({'sub':str(user),'exp':int(time.time())+3600})
    token = data+'.'+base64.urlsafe_b64encode(hmac.new(private['JWT_SECRET'].encode(),data.encode(),hashlib.sha256).digest()).decode().rstrip('=')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def api(path, body=None, authenticated=True):
        request=urllib.request.Request(args.url+path,data=json.dumps(body).encode() if body is not None else None,
            headers={'Content-Type':'application/json',**({'Authorization':'Bearer '+token} if authenticated else {})})
        try:
            with opener.open(request,timeout=50) as result: return result.status,json.load(result)
        except urllib.error.HTTPError as error:
            raw=error.read()
            return error.code,json.loads(raw) if raw[:1]==b'{' else {}
    cases=[]
    args.output.mkdir(parents=True,exist_ok=True)
    def check(name, passed):
        cases.append({'case':name,'passed':bool(passed)})
        (args.output/'cases.json').write_text(json.dumps(cases,ensure_ascii=False,indent=2))
        if not passed: raise AssertionError(name)
    check('public ping',api('/api/v1/ping',authenticated=False)[0]==200)
    check('chat JWT required',api('/api/v1/agent/chat',{'message':'查询'},False)[0]==401)
    check('knowledge JWT required',api('/api/v1/agent/knowledge/K'+'0'*20,authenticated=False)[0]==401)
    check('forged identity rejected',api('/api/v1/agent/chat',{'message':'查询','user_id':999})[0]==400)
    check('invalid knowledge ID',api('/api/v1/agent/knowledge/invalid')[0]==400)
    check('unknown knowledge ID or unavailable upstream',api('/api/v1/agent/knowledge/K'+'0'*20)[0]==(503 if args.fault_check else 404))
    check('internal denied',api('/internal/agent/health',authenticated=False)[0]==403)
    if args.fault_check:
        check('chat down isolated',api('/api/v1/agent/chat',{'message':'确认告警怎么操作？'})[0]==503)
        for path in ('/api/v1/devices','/api/v1/alarms','/api/v1/dashboard/summary','/api/v1/predictions'):
            check('business available '+path,api(path)[0]==200)
        return
    session = None
    questions = ['确认告警和已恢复有什么区别？','设备历史曲线为空应该怎么排查？',
        '知识库未命中后如何处理？','查询所有设备列表，附证据。',
        '查询设备1的预测，并说明预测结果过期应该怎么处理。','风机覆冰应该怎么排查？',
        '当前有哪些告警，怎么查看？']
    for index, question in enumerate(questions):
        if args.case is not None and args.case != index:
            continue
        started=time.monotonic()
        code, envelope=api('/api/v1/agent/chat',{'message':question,**({'session_id':session} if index==1 and session else {})})
        check('chat response '+str(index),code==200)
        response=envelope['data']
        (args.output/f'answer-{index}.json').write_text(json.dumps({'question':question,'elapsed_ms':round((time.monotonic()-started)*1000),'response':response},ensure_ascii=False,indent=2))
        check('safe answer status '+str(index),response['status'] in ('answered','unable_to_determine','degraded'))
        if index in (0,1,2,3): check('real answered '+str(index),response['status']=='answered')
        if index==4:
            answered, prediction_cited, document_cited = mixed_answer_checks(response)
            check('mixed question answered',answered)
            check('mixed prediction cited in conclusion',prediction_cited)
            check('mixed document cited in answer',document_cited)
        available={e['id'] for e in response['evidence'] if e['status']=='ok'}
        for claim in ([response['conclusion']] if response['conclusion'] else [])+response['suggestions']:
            check('valid citation '+str(index),bool(claim['evidence_ids']) and set(claim['evidence_ids'])<=available)
        if index==0:
            session=response['session_id']
            source=next(e for e in response['evidence'] if e.get('kind')=='document')
            check('source readable',api(source['source'])[0]==200)
        if index==1: check('same user followup',response['session_id']==session)
        if index==5:
            check('miss correctly reported',response['knowledge_status']=='miss')
            check('miss durably recorded',response['miss_record_status']=='recorded')
            check('configured search evidence' if args.search_configured else 'search explicitly degraded',
                  response['web_status']==('hit' if args.search_configured else 'not_configured'))
        if index==6:
            check('live alarm also searches knowledge',response['knowledge_status']=='hit')
            if response['status']=='answered':
                cited=set(response['conclusion']['evidence_ids'])
                check('live alarm conclusion cites current alarm tool',any(e['id'] in cited and e['status']=='ok'
                      and e['kind']=='business' and e['tool']=='list_alarms' for e in response['evidence']))
            else:
                check('live alarm fails closed',response['conclusion'] is None and not response['suggestions'])
    print(json.dumps({'cases':len(cases),'passed':sum(c['passed'] for c in cases)}))


if __name__=='__main__': main()
