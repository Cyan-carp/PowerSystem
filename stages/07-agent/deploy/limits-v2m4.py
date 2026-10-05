"""Server JWT/role and ten-admission checks without any model/search call."""
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


def main(args):
    args.output.mkdir(parents=True,exist_ok=True)
    private=dict(line.split('=',1) for line in (args.project/'.env').read_text().splitlines() if '=' in line and not line.startswith('#'))
    def sql(query):return subprocess.check_output(['docker','exec','powersystem-stage4-postgres-1','psql','-U','powersystem','-d','powersystem','-Atqc',query],text=True).strip()
    users={role:int(sql("SELECT id FROM users WHERE role='"+role+"' ORDER BY id LIMIT 1").splitlines()[0]) for role in ('admin','operator')}
    def token(role,expiry=3600):
        encode=lambda data:base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip('=')
        raw=encode({'alg':'HS256','typ':'JWT'})+'.'+encode({'sub':str(users[role]),'exp':int(time.time())+expiry})
        return raw+'.'+base64.urlsafe_b64encode(hmac.new(private['JWT_SECRET'].encode(),raw.encode(),hashlib.sha256).digest()).decode().rstrip('=')
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def api(path,auth,body=None):
        req=urllib.request.Request(args.url+path,data=json.dumps(body).encode() if body is not None else None,
            headers={'Content-Type':'application/json','Authorization':'Bearer '+auth})
        try:
            with opener.open(req,timeout=50) as result:return result.status,dict(result.headers),json.load(result)
        except urllib.error.HTTPError as error:
            raw=error.read();return error.code,dict(error.headers),json.loads(raw) if raw[:1]==b'{' else {}
    cases=[]
    def check(name,passed):
        cases.append({'case':name,'passed':bool(passed)})
        (args.output/'limits.json').write_text(json.dumps({'completed':False,'passed':False,'cases':cases},indent=2))
        if not passed:raise AssertionError(name)
    check('invalid JWT',api('/api/v1/agent/interpretations','invalid')[0]==401)
    check('expired JWT',api('/api/v1/agent/interpretations',token('operator',-1))[0]==401)
    check('operator admin denial',api('/api/v1/agent/model-status',token('operator'))[0]==403)
    check('admin status',api('/api/v1/agent/model-status',token('admin'))[0]==200)
    check('forged user',api('/api/v1/agent/chat',token('operator'),{'message':'查询','user_id':999})[0]==400)
    # This unknown, non-public topic has no evidence and never calls a vendor.
    message='量子纠缠基础解释应该怎么阅读？'
    response=None
    for i in range(10):
        code,headers,body=api('/api/v1/agent/chat',token('operator'),{'message':message})
        check('accepted new session '+str(i),code==200 and body.get('data',{}).get('status')=='unable_to_determine')
        response=body['data']
    code,headers,_=api('/api/v1/agent/chat',token('operator'),{'message':message})
    check('new session eleventh denied',code==429 and int(headers.get('Retry-After','0'))>0)
    code,_,_=api('/api/v1/agent/chat',token('admin'),{'message':message,'session_id':response['session_id']})
    check('cross-user session denied',code==403)
    for path in ('/api/v1/devices','/api/v1/alarms','/api/v1/dashboard/summary','/api/v1/predictions','/api/v1/agent/interpretations'):
        check('business outside chat quota '+path,api(path,token('operator'))[0]==200)
    (args.output/'limits.json').write_text(json.dumps({'completed':True,'passed':True,'cases':cases,'vendor_calls':0},indent=2))
    print(json.dumps({'passed':True,'checks':len(cases)}))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--project',type=Path,default=Path('/opt/powersystem'));parser.add_argument('--url',required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.url=args.url.rstrip('/')
    main(args)
