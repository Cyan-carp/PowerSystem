"""Public-entry acceptance with existing accounts; credentials stay in memory."""
import argparse,asyncio,hashlib,json,os
from pathlib import Path
import httpx
import smoke

async def extra(args):
    checks={};details={}
    async with httpx.AsyncClient(base_url=args.base_url,timeout=135,trust_env=False) as client:
        login=await client.post('/api/v1/auth/login',json={'username':args.username,'password':Path(args.password_file).read_text().strip()})
        login.raise_for_status();token=login.json()['data']['token'];client.headers['Authorization']='Bearer '+token
        for label,path in [('devices','/api/v1/devices?page_size=100'),('alarms','/api/v1/alarms?page_size=10'),('telemetry','/api/v1/devices/2/telemetry/latest'),('prediction','/api/v1/devices/2/prediction'),('statistics','/api/v1/alarms/statistics'),('interpretations','/api/v1/agent/interpretations'),('dashboard','/api/v1/dashboard/summary')]:
            response=await client.get(path);checks[label]=response.status_code==200 and response.json().get('code')==0
            if label=='devices':details['devices']=response.json()['data']
        report=json.loads((args.output/'responses.json').read_text())
        context=next(v['response'] for v in report['cases'] if v['label']=='多轮指代')
        sid=context['session_id'];history=(await client.get('/api/v1/agent/sessions/'+sid)).json()['data']
        canonical=json.dumps(history,sort_keys=True,ensure_ascii=False)
        (args.output/'history-before.json').write_text(json.dumps({'session_id':sid,'sha256':hashlib.sha256(canonical.encode()).hexdigest(),'history':history},ensure_ascii=False,indent=2))
        checks['persisted_two_turns']=len(history['turns'])==2
        admin=await client.post('/api/v1/auth/login',json={'username':'p0-admin','password':Path(args.admin_password_file).read_text().strip()});admin.raise_for_status()
        other={'Authorization':'Bearer '+admin.json()['data']['token']}
        checks['cross_user_read_denied']=(await client.get('/api/v1/agent/sessions/'+sid,headers=other)).status_code==404
        checks['cross_user_delete_denied']=(await client.delete('/api/v1/agent/sessions/'+sid,headers=other)).status_code==404
        own=(await client.get('/api/v1/agent/sessions')).json()['data']
        checks['session_list_owns_history']=sid in str(own)
        source=next(s for v in report['cases'] for s in v['response'].get('sources',[]) if s['kind']=='document')
        checks['rag_source_details']=(await client.get('/api/v1/agent/rag/sources/'+source['id'])).status_code==200
        deletion=next(v['response']['session_id'] for v in report['cases'] if v['label']=='生活问答')
        checks['own_session_delete']=(await client.delete('/api/v1/agent/sessions/'+deletion)).status_code==200 and (await client.get('/api/v1/agent/sessions/'+deletion)).status_code==404
        client.headers.pop('Authorization')
        checks['anonymous_history_denied']=(await client.get('/api/v1/agent/sessions')).status_code==401
        checks['registration_closed']=(await client.post('/api/v1/auth/register',json={})).status_code==403
        checks['internal_blocked']=(await client.get('/internal/agent/sessions')).status_code==403
    (args.output/'regression.json').write_text(json.dumps({'passed':all(checks.values()),'checks':checks,'details':details},ensure_ascii=False,indent=2))
    print(json.dumps(checks,ensure_ascii=False),flush=True)
    return all(checks.values())

async def main(args):
    os.environ['NATURAL_TEST_USERNAME']=args.username
    os.environ['NATURAL_TEST_PASSWORD']=Path(args.password_file).read_text().strip()
    args.case=None
    passed=await smoke.main(args)
    return await extra(args) and passed

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--base-url',required=True);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--username',default='demo-operator');parser.add_argument('--password-file',required=True);parser.add_argument('--admin-password-file',required=True)
    raise SystemExit(0 if asyncio.run(main(parser.parse_args())) else 1)
