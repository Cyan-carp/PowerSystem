"""Fresh isolated restore only. No original credentials, Worker, or action replay."""
import argparse,hashlib,json,os,re,secrets,subprocess,time
from pathlib import Path
ROOT=Path('/opt/powersystem')
def run(argv,**kw):
    r=subprocess.run(argv,capture_output=True,**kw)
    if r.returncode:raise RuntimeError('isolated_ma_restore_command_failed:'+Path(argv[0]).name)
    return r.stdout
def sql(container,db,value):
    return run(['docker','exec','-i',container,'psql','-U','powersystem','-d',db,'-v','ON_ERROR_STOP=1','-Atq'],input=value.encode()).decode().strip()
def main(a):
    if os.uname().nodename!='R730XD':raise ValueError('specified_host_required')
    source=a.source.resolve()
    if not source.is_relative_to(Path('/var/backups/powersystem')) or source.name!='ma':raise ValueError('private_restore_source_required')
    pg=json.loads(run(['docker','inspect',a.postgres_container]))[0]
    project=pg['Config']['Labels'].get('com.docker.compose.project','')
    if not re.fullmatch('powersystem-restore-[0-9]+',project) or pg['Config']['Labels'].get('com.docker.compose.service')!='postgres':raise ValueError('fresh_restore_project_required')
    if time.time()-int(project.rsplit('-',1)[1])>900:raise ValueError('old_restore_project_blocked')
    private=source.parent/'ma-restore-runtime';private.mkdir(mode=0o700,exist_ok=False);os.chown(private,10003,10003)
    db='ma_m4_restore_'+project.rsplit('-',1)[1]
    password=secrets.token_urlsafe(36)
    sql(a.postgres_container,'postgres','CREATE DATABASE '+db+';')
    run(['docker','cp',str(source/'ma_ops.dump'),a.postgres_container+':/tmp/ma_ops.dump'])
    run(['docker','exec',a.postgres_container,'pg_restore','-U','powersystem','-d',db,'--no-owner','--no-privileges','/tmp/ma_ops.dump'])
    manifest=json.loads((source/'manifest.json').read_text())
    restored={t:int(sql(a.postgres_container,db,'SELECT count(*) FROM '+t+';')) for t in manifest['counts']}
    if restored!=manifest['counts']:raise ValueError('restore_counts_mismatch')
    baseline=json.loads((source/'task-baseline.json').read_text())
    actual=json.loads(sql(a.postgres_container,db,"SELECT coalesce(json_agg(json_build_object('task_id',task_id,'user_id',user_id,'status',status,'result',result)),'[]'::json)::text FROM ma_tasks;"))
    if sorted(actual,key=lambda x:x['task_id'])!=sorted(baseline,key=lambda x:x['task_id']):raise ValueError('restore_task_facts_mismatch')
    # Explicit recovery-only fixtures; no production or original task modifications.
    tid=str(__import__('uuid').uuid4());aid=str(__import__('uuid').uuid4());trace=str(__import__('uuid').uuid4());sid=str(__import__('uuid').uuid4())
    envelope=json.dumps({'actor':{'user_id':999001,'role':'admin'},'intent':'隔离恢复queued/approved fixture，禁止执行','operation':'restart_drill_container','constraints':{'max_risk_class':'R2'},'task_kind':'ops','user_request':'恢复fixture禁止重启'})
    fixtures="INSERT INTO ma_tasks(task_id,trace_id,session_id,user_id,parent_turn_id,idempotency_key,request_hash,envelope,status,deadline) VALUES('"+tid+"','"+trace+"','"+sid+"',999001,NULL,'"+tid+"','"+'a'*64+"','"+envelope+"'::jsonb,'queued',now()+interval '30 minutes');\n"
    fixtures+="INSERT INTO ma_task_steps(task_id,step_no,tool,args,args_hash,risk_class,status,execution_epoch) VALUES('"+tid+"',1,'docker.restart_drill_container','{}','"+'a'*64+"','R2','pending',0);\n"
    fixtures+="INSERT INTO ma_policy_versions(version,snapshot) VALUES('restore-fixture','{\"fixture\":true}');\n"
    fixtures+="INSERT INTO ma_approvals(approval_id,task_id,step_no,plan,args_hash,policy_version,status,expires_at) VALUES('"+aid+"','"+tid+"',1,'{\"fixture\":true}','"+'a'*64+"','restore-fixture','approved',now()+interval '30 minutes');\nINSERT INTO ma_outbox(task_id) VALUES('"+tid+"');\n"
    sql(a.postgres_container,db,fixtures)
    sql(a.postgres_container,db,"CREATE ROLE ma_restore_reader LOGIN PASSWORD '"+password+"' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT; GRANT CONNECT ON DATABASE "+db+" TO ma_restore_reader; GRANT USAGE ON SCHEMA public TO ma_restore_reader; GRANT SELECT ON ALL TABLES IN SCHEMA public TO ma_restore_reader; ALTER ROLE ma_restore_reader SET default_transaction_read_only=on;")
    for name in ('service-token','actor-key','envelope-key'):(private/name).write_text(secrets.token_urlsafe(36));os.chown(private/name,10003,10003);(private/name).chmod(0o600)
    redis_password=secrets.token_urlsafe(36)
    run(['docker','exec','-i',project+'-redis-1','redis-cli','-x','ACL','SETUSER','ma_restore_reader','reset','on','+ping'],input=('>'+redis_password).encode())
    env={'OPS_RUNTIME_PROFILE':'m4-restore-readonly','OPS_ENABLED':'false','OPS_WRITE_ENABLED':'false','OPS_INFRA_ENABLED':'false','OPS_SECURITY_ENABLED':'false','OPS_INSPECTION_ENABLED':'false','OPS_DATABASE_URL':'postgresql://ma_restore_reader:'+password+'@postgres-restore:5432/'+db,'OPS_REDIS_URL':'redis://ma_restore_reader:'+redis_password+'@redis-restore:6379/15','OPS_REDIS_PREFIX':'ma:restore:'+project+':','OPS_SERVICE_TOKEN_FILE':'/run/restore/service-token','MA_ACTOR_KEY_FILE':'/run/restore/actor-key','MA_ENVELOPE_KEY_FILE':'/run/restore/envelope-key'}
    (private/'ops.env').write_text(''.join(k+'='+v+'\n' for k,v in env.items()));(private/'ops.env').chmod(0o600)
    network=next(iter(pg['NetworkSettings']['Networks']));pgip=pg['NetworkSettings']['Networks'][network]['IPAddress']
    rd=json.loads(run(['docker','inspect',project+'-redis-1']))[0];rdip=rd['NetworkSettings']['Networks'][network]['IPAddress']
    name=project+'-ma-readonly';run(['docker','run','-d','--name',name,'--network',network,'--add-host','postgres-restore:'+pgip,'--add-host','redis-restore:'+rdip,'--user','10003:10003','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges:true','--tmpfs','/tmp','--env-file',str(private/'ops.env'),'-v',str(private)+':/run/restore:ro',manifest['images']['ops-agent']])
    probe="""
import json,time,urllib.request,urllib.error
from pathlib import Path
from ops_agent.security import sign
key=Path('/run/restore/actor-key').read_text().strip();token=Path('/run/restore/service-token').read_text().strip()
now=int(time.time());actor=sign({'aud':'ma-ops','kid':'ma-v1','iat':now,'exp':now+60,'user_id':999001,'role':'admin'},key)
headers={'X-PowerSystem-Token':token,'X-MA-Actor':actor,'Content-Type':'application/json'}
url='http://127.0.0.1:8094/internal/ma/v1/tasks/TASK_ID'
for _ in range(60):
 try:
  with urllib.request.urlopen('http://127.0.0.1:8094/health',timeout=2):break
 except OSError:time.sleep(1)
else:raise RuntimeError('restore_service_not_ready')
with urllib.request.urlopen(urllib.request.Request(url,headers=headers)) as response: report=json.load(response)
request=urllib.request.Request(url+'/cancel',data=b'{}',headers=headers,method='POST')
try:urllib.request.urlopen(request);raise RuntimeError('restore_write_not_blocked')
except urllib.error.HTTPError as e:assert e.code==403
print(json.dumps({'fixture_readable':report['status']=='queued','restore_post_blocked':True,'can_decide':any(a.get('can_decide',False) for a in report['approvals'])}))
"""
    probe=probe.replace('TASK_ID',tid)
    checks=json.loads(run(['docker','exec','-i',name,'python','-'],input=probe.encode()))
    time.sleep(2)
    counts_after={t:int(sql(a.postgres_container,db,'SELECT count(*) FROM '+t+';')) for t in ('ma_execution_attempts','ma_tool_calls','ma_security_reviews','ma_outbox')}
    checks.update(no_new_attempts=counts_after['ma_execution_attempts']==manifest['counts']['ma_execution_attempts'],no_tool_replay=counts_after['ma_tool_calls']==manifest['counts']['ma_tool_calls'],no_model_replay=counts_after['ma_security_reviews']==manifest['counts']['ma_security_reviews'],no_outbox_replay=counts_after['ma_outbox']==manifest['counts']['ma_outbox']+1,facts_equal=True,counts_equal=True,production_credentials=False)
    (private/'checks.json').write_text(json.dumps(checks,indent=2));(private/'checks.json').chmod(0o600)
    if not all(v for k,v in checks.items() if k not in ('can_decide','production_credentials')) or checks['can_decide']:raise ValueError('restore_safety_failed')
    print(json.dumps({'ma_restore_passed':True,'checks':checks,'project':project}))
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True);p.add_argument('--postgres-container',required=True);main(p.parse_args())
