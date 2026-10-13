"""Create only this new local Compose drill project; keep all earlier containers unchanged."""
import argparse
import hashlib
import json
import shutil
import subprocess
import time
from datetime import datetime,timezone
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'stages/09-ops-agent'))
from ops_contract import digest


def main(args):
    run=(ROOT/args.run).resolve();private=run/'private'
    if not run.is_relative_to(ROOT/'artifacts/stage9-ops-agent') or not (private/'local.env').is_file():raise ValueError('prepared_m3_batch_required')
    if (run/'remote/drill.json').exists():raise ValueError('new_drill_batch_required')
    project='powersystem-ma-m3-'+hashlib.sha256(str(run).encode()).hexdigest()[:8]
    def docker(*argv):
        r=subprocess.run(['docker',*argv],capture_output=True,text=True,encoding='utf-8')
        if r.returncode: raise RuntimeError('owned_docker_command_failed: '+r.stderr[-2000:])
        return r.stdout.strip()
    baseline=[json.loads(s) for s in docker('ps','-a','--no-trunc','--format','json').splitlines()]
    (run/'remote').mkdir(exist_ok=True)
    (run/'remote/baseline.json').write_text(json.dumps(baseline,indent=2),encoding='utf-8')
    broker_private=private/'broker';broker_private.mkdir(exist_ok=True)
    for src,dst in (('executor-write-key','write-key'),('executor-grant-key','read-key')):shutil.copyfile(private/src,broker_private/dst)
    common={'restart':'no','read_only':True,'cap_drop':['ALL'],'security_opt':['no-new-privileges:true'],'labels':{'powersystem.milestone':'MA-M3'},'networks':['drill-only']}
    compose={'name':project,'services':{
      'drill':{**common,'build':{'context':str(ROOT/'stages/09-ops-agent'),'dockerfile':'deploy/drill.Dockerfile'}},
      'broker':{**common,'build':{'context':str(ROOT/'stages/09-ops-agent'),'dockerfile':'deploy/drill-broker.Dockerfile'},
          'networks':['control'],
          'ports':['127.0.0.1:8097:8090'],'volumes':[str(broker_private)+':/private:ro','/var/run/docker.sock:/var/run/docker.sock', 'broker-state:/state']},
      'redis':{'image':'redis:7.4.5-alpine','restart':'no','labels':{'powersystem.milestone':'MA-M3'},
          'ports':['127.0.0.1:16377:6379'],'volumes':[str(private/'redis.conf')+':/usr/local/etc/redis/redis.conf:ro'],
          'command':['redis-server','/usr/local/etc/redis/redis.conf'],'networks':['control']}},
      'networks':{'drill-only':{'internal':True},'control':{}},'volumes':{'broker-state':{}}}
    path=private/'compose.json';path.write_text(json.dumps(compose,indent=2),encoding='utf-8')
    # Refuse to adopt any existing project objects. Never remove or replace other containers.
    if docker('ps','-a','-q','--filter','label=com.docker.compose.project='+project):raise ValueError('project_already_exists')
    docker('compose','-f',str(path),'up','-d','--build','drill','redis')
    cid=docker('compose','-f',str(path),'ps','-q','drill')
    until=time.monotonic()+30
    while True:
        info=json.loads(docker('inspect',cid))[0]
        if info['State'].get('Health',{}).get('Status')=='healthy':break
        if time.monotonic()>=until:raise ValueError('drill_health_timeout')
        time.sleep(.5)
    cid=info['Id'];image=info['Image'];network=info['HostConfig']['NetworkMode']
    stamp=datetime.now(timezone.utc).isoformat()
    def asset(aid,kind,parent,capabilities):
        return dict(asset_id=aid,kind=kind,display_name='本机隔离演练容器' if parent else '本机Docker演练环境',
            project_scope='powersystem',parent_id=parent,enabled=True,criticality='drill',environment='acceptance',
            version=1,verified_at=stamp,connection_ref='local-drill',identity_hash=digest({'project':project,'container_id':cid} if parent else {'project':project}),capabilities=capabilities)
    assets=[asset('local-drill-host','host',None,[]),asset('ma-m3-drill','container','local-drill-host',['docker.inspect_container','docker.restart_drill_container'])]
    registry={'assets':assets}
    (private/'registry.json').write_text(json.dumps(registry,ensure_ascii=False,indent=2),encoding='utf-8')
    policy={'project':project,'network':network,'tool':'docker.restart_drill_container','version':'1.0','grace_seconds':10,
            'tool_timeout_seconds':15,'asset_id':'ma-m3-drill','criticality':'drill','environment':'acceptance'}
    broker={'asset_id':'ma-m3-drill','container_id':cid,'image_digest':image,'registry':registry,'policy':policy,
            'write_enabled':True,'window_until':time.time()+4*3600,'window_file':'/private/write-window.json','write_key_file':'/private/write-key','read_key_file':'/private/read-key','state_file':'/state/attempts.sqlite'}
    (broker_private/'write-window.json').write_text(json.dumps({'write_enabled':True,'window_until':broker['window_until']}),encoding='utf-8')
    (broker_private/'broker.json').write_text(json.dumps(broker,ensure_ascii=False,indent=2),encoding='utf-8')
    executor={'token_file':str(private/'executor-token'),'grant_key_file':str(private/'executor-grant-key'),
        'write_key_file':str(private/'executor-write-key'),'registry_file':str(private/'registry.json'),
        'callback_url':'http://127.0.0.1:8098','hosts':{},'ssh_executable':str(Path('C:/Windows/System32/OpenSSH/ssh.exe')),'drill_url':'http://127.0.0.1:8097'}
    (private/'executor.json').write_text(json.dumps(executor,indent=2),encoding='utf-8')
    docker('compose','-f',str(path),'up','-d','--build','broker')
    summary={'project':project,'container_id':cid,'image_digest':image,'registry_hash':digest(assets),'policy_version':digest(policy),'network':network,'window_until':broker['window_until'],'compose_file':str(path)}
    (run/'remote/drill.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print('MA-M3 isolated drill, broker and authenticated Redis created; original containers retained.')


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--run',required=True);main(p.parse_args())
