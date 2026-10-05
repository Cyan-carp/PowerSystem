"""Explicit R730xd acceptance, synthetic assets only; no secret output."""
import argparse
import base64
import hashlib
import hmac
import json
import subprocess
import time
import urllib.request
import urllib.error
from datetime import datetime,timezone
from pathlib import Path
from uuid import uuid4

PROJECT=Path('/opt/powersystem')
OUTPUT=PROJECT/'runtime/v2m2-20261001'
def sql(query):
    result=subprocess.run(['docker','exec','-i','powersystem-stage4-postgres-1','psql','-U','powersystem','-d','powersystem','-At','-v','ON_ERROR_STOP=1'],input=query,text=True,capture_output=True)
    if result.returncode:raise RuntimeError('acceptance SQL failed')
    return result.stdout.strip()

def main(cleanup=False):
    if not (PROJECT/'.env').is_file():raise RuntimeError('expected explicit R730xd project')
    private=dict(line.split('=',1) for line in (PROJECT/'.env').read_text().splitlines() if '=' in line and not line.startswith('#'))
    def jwt(user):
        enc=lambda value:base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip('=')
        data=enc({'alg':'HS256','typ':'JWT'})+'.'+enc({'sub':str(user),'exp':int(time.time())+3600})
        return data+'.'+base64.urlsafe_b64encode(hmac.new(private['JWT_SECRET'].encode(),data.encode(),hashlib.sha256).digest()).decode().rstrip('=')
    admin=jwt(int(sql("SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1").splitlines()[0]))
    operator=jwt(int(sql("SELECT id FROM users WHERE role='operator' ORDER BY id LIMIT 1").splitlines()[0]))
    cases=[]
    def check(name,passed):
        cases.append({'case':name,'passed':bool(passed)})
        OUTPUT.joinpath('acceptance-cases.json').write_text(json.dumps(cases,ensure_ascii=False,indent=2))
        if not passed:raise AssertionError(name)
    def api(method,path,body=None,token=admin):
        request=urllib.request.Request('https://8.138.10.222'+path,data=json.dumps(body).encode() if body is not None else None,headers={'Content-Type':'application/json',**({'Authorization':'Bearer '+token} if token else {})},method=method)
        try:
            with urllib.request.urlopen(request,timeout=50) as response:return response.status,json.load(response)
        except urllib.error.HTTPError as error:
            raw=error.read();return error.code,json.loads(raw) if raw[:1]==b'{' else {}
    state=OUTPUT/'acceptance-state.json'
    if cleanup:
        data=json.loads(state.read_text())
        sql('UPDATE alarm_records SET status=\'recovered\',recovered_at=now() WHERE id IN ('+','.join(map(str,data['alarm_ids']))+'); UPDATE devices SET deleted_at=now() WHERE id IN ('+','.join(map(str,data['devices']))+'); UPDATE alarm_rules SET enabled=false WHERE id IN ('+','.join(map(str,data['rules']))+');')
        for key in data['monitor_keys']:sql("UPDATE agent_monitor_events SET recovered_at=now() WHERE event_key='"+key+"'")
        print('Synthetic acceptance events recovered; devices soft deleted; rules disabled.')
        return
    check('public ping',api('GET','/api/v1/ping',token='')[0]==200)
    check('JWT required',api('GET','/api/v1/agent/interpretations',token='')[0]==401)
    check('operator model permission',api('GET','/api/v1/agent/model-status',token=operator)[0]==403)
    check('internal denied',api('GET','/internal/agent/model-status',token='')[0]==403)
    check('admin model probe',api('POST','/api/v1/agent/model-probe',{})[0]==200)
    check('admin probe limited',api('POST','/api/v1/agent/model-probe',{})[0]==429)
    devices=[];rules=[];alarm_ids=[];monitor_keys=[];run=uuid4().hex[:8]
    def save_state():
        state.write_text(json.dumps({'devices':devices,'rules':rules,'alarm_ids':alarm_ids,'monitor_keys':monitor_keys,'run_id':run}));state.chmod(0o600)
    save_state()
    for index in range(3):
        code='INV-V2M2-SERVER-'+run+'-'+str(index)
        status,response=api('POST','/api/v1/devices',{'device_code':code,'name':'V2-M2 server synthetic test','dev_type':'inverter','vendor':'synthetic','station_code':'ST-V2M2','group_name':'v2m2-server-test'})
        check('create test device '+str(index),status==201);device=response['data']['id'];devices.append(device)
        status,response=api('POST','/api/v1/alarm-rules',{'device_id':device,'metric':'temperature','operator':'>','threshold':60,'level':'major'})
        check('create test rule '+str(index),status==201);rules.append(response['data']['id'])
        save_state()
        payload={'schema_version':1,'run_id':'v2m2-'+run,'device_id':code,'station_id':'ST-V2M2','seq':0,'ts_ms':int(time.time()*1000),'voltage':400,'current':10,'temperature':70,'power':6.86,'status':1,'fault_code':0}
        started=time.monotonic()
        subprocess.run(['docker','exec','-i','powersystem-stage4-simulator-1','python','-c',"import sys;from paho.mqtt.publish import single;single('device/telemetry',sys.stdin.read(),qos=1,hostname='emqx')"],input=json.dumps(payload),text=True,check=True,stdout=subprocess.DEVNULL)
        for _ in range(30):
            raw=sql(f"SELECT id FROM alarm_records WHERE device_id={device} AND metric='temperature'")
            if raw:break
            time.sleep(.2)
        check('original alarm under 10s '+str(index),bool(raw) and time.monotonic()-started<10);alarm_ids.append(int(raw.splitlines()[0]))
        sql(f"INSERT INTO prediction_records(device_id,window_end_ms,probability,threshold,risk_level,model_version,top_factors,source) VALUES({device},{int(time.time()*1000)},0.72,0.65,'high','v2m2-server-test','[]'::jsonb,'synthetic-trained');")
        raw=sql(f"INSERT INTO alarm_records(device_id,metric,level,value,threshold,status,triggered_at) VALUES({device},'ai_failure_risk','major',0.72,0.65,'unhandled',now()) RETURNING id")
        alarm_ids.append(int(raw.splitlines()[0]))
        starts=datetime.now(timezone.utc).isoformat();fingerprint='v2m2-server-'+run+'-'+str(index)
        batch={'version':'4','groupKey':'v2m2-server','truncatedAlerts':0,'status':'firing','receiver':'agent','groupLabels':{},'commonLabels':{},'commonAnnotations':{},'externalURL':'','alerts':[{'status':'firing','labels':{'alertname':'PowerSystemAPIDown','severity':'critical','test_run':run},'annotations':{'summary':'合成服务器链路验收：API 抓取失败'},'startsAt':starts,'endsAt':'0001-01-01T00:00:00Z','fingerprint':fingerprint}]}
        script="import sys,httpx;from agent.config import Config;c=Config.load();r=httpx.post('http://127.0.0.1:8092/internal/agent/monitor-events',headers={'Authorization':'Bearer '+c.monitor_token},content=sys.stdin.read(),timeout=5,trust_env=False);assert r.status_code==200"
        subprocess.run(['docker','exec','-i','powersystem-stage4-agent-1','python','-c',script],input=json.dumps(batch),text=True,check=True)
        monitor_keys.append('monitor:'+fingerprint+':'+datetime.fromisoformat(starts).strftime('%Y-%m-%dT%H:%M:%S.%f').rstrip('0')+'Z')
        save_state()
    state.write_text(json.dumps({'devices':devices,'rules':rules,'alarm_ids':alarm_ids,'monitor_keys':monitor_keys,'run_id':run}));state.chmod(0o600)
    keys=['alarm:'+str(identifier) for identifier in alarm_ids]+monitor_keys
    for _ in range(120):
        rows=json.loads(sql("SELECT json_agg(json_build_object('id',id,'event_key',event_key,'category',category,'task_status',task_status,'result',result,'evidence',evidence)) FROM agent_interpretations WHERE event_key IN ("+','.join("'"+key+"'" for key in keys)+")") or '[]')
        if len(rows)==9 and all(row['task_status'] in ('completed','degraded') for row in rows):break
        time.sleep(1)
    for row in rows:check('real server answer '+row['event_key'],row['result']['status']=='answered')
    check('nine server interpretations',len(rows)==9)
    identifier=rows[0]['id'];check('public result detail',api('GET','/api/v1/agent/interpretations/'+str(identifier))[0]==200)
    check('public read receipt',api('POST','/api/v1/agent/interpretations/'+str(identifier)+'/read',{})[0]==200)
    check('business notification created',int(sql("SELECT count(*) FROM business_notification_outbox WHERE event_key IN ("+','.join("'alarm:"+str(identifier)+":triggered'" for identifier in alarm_ids)+")"))>=3)
    OUTPUT.joinpath('acceptance.json').write_text(json.dumps({'cases':cases,'records':rows,'run_id':run},ensure_ascii=False,indent=2))
    print('Server public acceptance:',len(cases),'checks, nine real-model answers; synthetic assets remain for browser verification.')

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--cleanup',action='store_true');main(parser.parse_args().cleanup)
