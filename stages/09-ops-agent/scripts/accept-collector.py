"""Native SSH/tool/privilege checks. Synthetic grants are explicit, separate from real task acceptance."""
import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4


async def main(args):
    root=Path(__file__).resolve().parents[3];sys.path.insert(0,str(root/'stages/09-ops-agent'))
    from ops_contract import TOOLS,digest,sign_grant,validate_args,validate_result
    from ops_executor.main import AdapterConfig,ssh_collect,child_environment
    if args.output.exists():raise ValueError('new_collector_batch_required')
    args.output.mkdir(parents=True);cfg=AdapterConfig(args.config)
    assets=cfg.registry['assets'];by_id={a['asset_id']:a for a in assets};records=[];checks={}
    def request(tool,aid,args_extra=None,overrides=None):
        arguments=validate_args(tool,{'asset_id':aid,**(args_extra or {})});now=int(time.time())
        grant={'aud':'ma-executor','kid':'ma-exec-v1','iat':now,'exp':now+30,'task_id':str(uuid4()),'trace_id':str(uuid4()),
            'execution_epoch':1,'step_no':1,'call_id':str(uuid4()),'user_id':91001,'role':'operator','tool':tool,
            'args_hash':digest(arguments),'asset_id':aid,'asset_version':by_id[aid]['version'],
            'registry_hash':digest(assets),'deadline':time.time()+300,**(overrides or {})}
        return {'protocol':'ma-exec/v1','grant':sign_grant(grant,cfg.grant_key),'tool':tool,'args':arguments}
    host=by_id['r730xd'];transport=cfg.hosts[host['connection_ref']]
    def native(body,command=None,forward=False):
        argv=[cfg.ssh,'-T','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','UpdateHostKeys=no','-o','IdentitiesOnly=yes',
              '-o','IdentityAgent=none','-o','ConnectTimeout=5','-o','UserKnownHostsFile='+transport['known_hosts_file'],
              '-i',transport['private_key_file'],'-p',str(transport['port'])]
        if forward:argv+=['-o','ExitOnForwardFailure=yes','-W','127.0.0.1:22']
        argv+=['powersystem-ops-read@'+transport['hostname']]
        if command:argv.append(command)
        completed=subprocess.run(argv,input=json.dumps(body).encode(),capture_output=True,env=child_environment(),timeout=20)
        # Never export private SSH paths or original stderr; only classifiable outcomes.
        parsed=json.loads(completed.stdout) if completed.stdout.strip().startswith(b'{') else None
        return {'exit':completed.returncode,'response':parsed,'forward_denied':b'forwarding failed' in completed.stderr or b'administratively prohibited' in completed.stderr}
    try:
        for tool,(kind,_) in TOOLS.items():
            aid='r730xd' if kind=='host' else 'r730xd-api' if kind=='container' else 'r730xd-business-db' if kind=='database' else next(a['asset_id'] for a in assets if a['kind']=='service')
            started=time.monotonic();body=request(tool,aid)
            reply=await ssh_collect(cfg,by_id[aid],body);validate_result(tool,reply)
            checks[tool]=reply['status']=='ok'
            records.append({'tool':tool,'reply':reply,'seconds':round(time.monotonic()-started,3),'synthetic_grant':True})
            print(tool,reply['status'],flush=True)
        # Replay uses the same completed read ID; the second request cannot read again.
        body=request('linux.get_memory_usage','r730xd')
        first=native(body);second=native(body)
        checks['native_replay_rejected']=first['response']['status']=='ok' and second['response']['error']['code']=='dispatch_replayed'
        checks['native_arbitrary_command_rejected']=native({},'id')['exit']!=0
        checks['native_unregistered_sudo_docker_rejected']=native({},'sudo -n /usr/bin/docker version')['exit']!=0
        # Write-tool names are tested only as signed RPCs, never production shell restart commands.
        write=request('linux.get_memory_usage','r730xd',overrides={'tool':'docker.restart_drill_container'})
        write['tool']='docker.restart_drill_container'
        checks['native_write_rpc_rejected']=native(write)['response']['error']['code']=='invalid_execution_grant'
        expired=request('linux.get_memory_usage','r730xd',overrides={'exp':int(time.time())-1})
        checks['native_expired_grant_rejected']=native(expired)['response']['error']['code']=='invalid_execution_grant'
        changed=request('linux.get_memory_usage','r730xd');changed['args']['path']='/etc/shadow'
        checks['native_extra_args_rejected']=native(changed)['response']['error']['code']=='invalid_args'
        scope=request('linux.get_memory_usage','r730xd',overrides={'registry_hash':'b'*64})
        checks['native_registry_mismatch_rejected']=native(scope)['response']['error']['code']=='asset_version_changed'
        forward=native({},forward=True);checks['native_forwarding_rejected']=forward['forward_denied'] or forward['exit']!=0
        # Independent admin observation reads only metadata/aggregates and permission booleans.
        probe=r'''
import hashlib,json,pathlib,subprocess,sys
sys.path.insert(0,'/opt/powersystem-ops-read/code')
from collector.broker import Collector
registry=json.loads(pathlib.Path('/etc/powersystem-ops-read/registry.json').read_text());collector=Collector(registry)
assets={a['asset_id']:a for a in registry['assets']};p=registry['private_assets']['r730xd-business-db']
info=collector.docker.inspect(registry['private_assets']['r730xd-postgres']['identity'])
address=info['NetworkSettings']['Networks'][p['network']]['IPAddress']
import psycopg
with psycopg.connect(host=address,port=5432,dbname='powersystem',user='ma_ops_diagnostics',password=pathlib.Path(p['password_file']).read_text().strip()) as conn:
    row=conn.execute("SELECT has_table_privilege(current_user,'public.users','SELECT'),has_table_privilege(current_user,'public.users','INSERT'),has_table_privilege(current_user,'public.users','UPDATE'),has_table_privilege(current_user,'public.users','DELETE'),has_schema_privilege(current_user,'public','CREATE'),pg_has_role(current_user,'pg_read_all_stats','MEMBER'),pg_has_role(current_user,'pg_signal_backend','MEMBER')").fetchone()
    functions=conn.execute("SELECT has_function_privilege(current_user,'ma_ops_monitor.db_status()','EXECUTE'),has_function_privilege(current_user,'ma_ops_monitor.connection_stats()','EXECUTE')").fetchone()
native_memory={k:int(v.split()[0])*1024 for k,v in (line.split(':',1) for line in pathlib.Path('/proc/meminfo').read_text().splitlines())}
containers=[]
for a in registry['assets']:
    if a['kind']=='container':
        item=collector.docker.inspect(registry['private_assets'][a['asset_id']]['identity'])
        containers.append({'asset_id':a['asset_id'],'container_id':item['Id'],'image_id':item['Image'],'started_at':item['State']['StartedAt'],'restart_count':item['RestartCount']})
print(json.dumps({'diagnostic_forbidden_privileges':list(row),'diagnostic_function_execute':list(functions),
                  'native_memory_total':native_memory['MemTotal'],'native_logical_cpus':__import__('os').cpu_count(),'containers':containers}))
'''
        admin=[cfg.ssh,'-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','UpdateHostKeys=no','-o','ConnectTimeout=5',
               '-o','UserKnownHostsFile='+transport['known_hosts_file'],
               '-i',str(Path.home()/'.ssh/id_ed25519'),'-p',str(transport['port']),'root@'+transport['hostname'],'/opt/powersystem-ops-read/venv/bin/python','-']
        completed=subprocess.run(admin,input=probe,capture_output=True,text=True,encoding='utf-8',timeout=30)
        if completed.returncode:raise ValueError('independent_permission_probe_failed')
        independent=json.loads(completed.stdout)
        (args.output/'independent-private.json').write_text(json.dumps(independent,ensure_ascii=False,indent=2),encoding='utf-8')
        checks['db_no_business_or_broad_stats_permissions']=not any(independent['diagnostic_forbidden_privileges'])
        checks['db_only_diagnostic_grants']=all(independent['diagnostic_function_execute'])
        memory=next(r['reply']['data'] for r in records if r['tool']=='linux.get_memory_usage')
        cpu=next(r['reply']['data'] for r in records if r['tool']=='linux.get_cpu_usage')
        checks['independent_host_container_vantage']=memory['total_bytes']==independent['native_memory_total'] and cpu['logical_cpus']==independent['native_logical_cpus']
        initial=json.loads(Path(args.baseline).read_text(encoding='utf-8'))
        before={c['container_id']:(c['image_id'],c['started_at'],c['restart_count']) for c in initial['containers']}
        after={c['container_id']:(c['image_id'],c['started_at'],c['restart_count']) for c in independent['containers']}
        checks['project_business_images_and_start_baseline_unchanged']=before==after
    finally:
        (args.output/'records.json').write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf-8')
        (args.output/'checks.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(checks,ensure_ascii=False),flush=True)
    if not checks or not all(checks.values()):raise SystemExit('Native collector acceptance incomplete; retain this batch.')


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('--baseline',required=True);p.add_argument('--output',type=Path,required=True)
    asyncio.run(main(p.parse_args()))
