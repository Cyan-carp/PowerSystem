"""Prepare one SEC-M3 local batch. Creates only its own databases/Compose objects."""
import argparse
import asyncio
import hashlib
import json
import secrets
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[3]


def read_env(path):
    return dict(line.split('=', 1) for line in Path(path).read_text(encoding='utf-8-sig').splitlines() if '=' in line and not line.startswith('#'))


def docker(*argv):
    r = subprocess.run(['docker', *argv], capture_output=True, text=True, encoding='utf-8')
    if r.returncode: raise RuntimeError('owned_docker_command_failed')
    return r.stdout.strip()


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


async def main(args):
    run = (ROOT / args.run).resolve(); private = run / 'private'
    if not run.is_relative_to(ROOT / 'artifacts/stage10-security-agent') or not run.name.startswith('sec-m3-'):
        raise ValueError('isolated_sec_m3_batch_required')
    project = 'powersystem-sec-m3-' + hashlib.sha256(str(run).encode()).hexdigest()[:8]
    if args.phase == 'config':
        private.mkdir(exist_ok=False)
        for port in (15434, 16379, 18105, 18097, 8110, 8111, 8112, 8113, 15179):
            with socket.socket() as s:
                s.bind(('127.0.0.1', port))
        source = read_env(ROOT/'artifacts/stage9-ops-agent/m3-20261012-r1/private/local.env')
        sec = read_env(ROOT/'artifacts/stage10-security-agent/sec-m1-20261013-r1/private/local.env')
        for name in ('pg-password','redis-password','ops-token','actor-key','envelope-key','executor-token',
                     'executor-grant-key','executor-write-key','jwt-secret','admin-password','operator-password',
                     'sec-service-token','sec-request-key','sec-result-key'):
            (private/name).write_text(secrets.token_urlsafe(36),encoding='utf-8')
        password=(private/'pg-password').read_text().strip();rp=(private/'redis-password').read_text().strip()
        dsn=lambda db:'postgresql://sec_owner:'+quote(password,safe='')+'@127.0.0.1:15434/'+db
        redis=lambda db:'redis://:'+quote(rp,safe='')+'@127.0.0.1:16379/'+str(db)
        env={k:v for k,v in source.items() if k.startswith(('AGENT_', 'RAG_', 'BND_', 'TDENGINE_'))}
        env.update(OPS_ENABLED='true',OPS_INFRA_ENABLED='true',OPS_WRITE_ENABLED='false',OPS_SECURITY_ENABLED='true',
            OPS_INSPECTION_ENABLED='true',OPS_DATABASE_URL=dsn('sec_m3_acceptance_test'),OPS_REDIS_URL=redis(4),OPS_REDIS_PREFIX='ma:sec-m3-acceptance:',
            OPS_MODEL='deepseek-v4-flash-0731',OPS_LLM_URL='https://hgapi.dieqiyun.top/v1',OPS_LLM_API_KEY_FILE=source['OPS_LLM_API_KEY_FILE'],
            OPS_BACKEND_URL='http://127.0.0.1:18105',OPS_SUPERVISOR_URL='http://127.0.0.1:18097',
            OPS_URL='http://127.0.0.1:8111',OPS_EXECUTOR_URL='http://127.0.0.1:8112',OPS_SECURITY_URL='http://127.0.0.1:8110',
            OPS_EXECUTOR_CONFIG_FILE=str(private/'executor.json'),OPS_AUDIT_DIR=str(run/'ops-audit'),
            RAG_DATABASE_URL=dsn('sec_m3_rag_acceptance_test'),AGENT_REDIS_URL=redis(5),
            AGENT_BACKEND_URL='http://127.0.0.1:18105',AGENT_CHAT_URL='http://127.0.0.1:18097',
            AGENT_LLM_MODEL='deepseek-v4.1-flash',AGENT_LLM_BASE_URL='https://hgapi.dieqiyun.top/v1',
            AGENT_SERVICE_TOKEN_FILE=str(private/'ops-token'),RAG_AUDIT_DIR=str(run/'rag-audit'),
            POSTGRES_HOST='127.0.0.1',POSTGRES_PORT='15434',POSTGRES_USER='sec_owner',POSTGRES_PASSWORD=password,POSTGRES_DB='ma_m3_business_test',
            REDIS_ADDR='127.0.0.1:16379',REDIS_PASSWORD=rp,HTTP_ADDR='127.0.0.1:18105',
            JWT_SECRET=(private/'jwt-secret').read_text().strip(),MA_DRILL_ACCEPTANCE_ONLY='true',
            MA_M3_ADMIN_PASSWORD_FILE=str(private/'admin-password'),MA_M3_OPERATOR_PASSWORD_FILE=str(private/'operator-password'),
            SEC_ENABLED='true',SEC_MODEL='deepseek-v4-flash-0731',SEC_LLM_URL='https://hgapi.dieqiyun.top/v1',
            SEC_LLM_API_KEY_FILE=sec['SEC_LLM_API_KEY_FILE'],SEC_MODEL_TIMEOUT_SECONDS='30',SEC_REVIEW_TTL_SECONDS='300',SEC_ACCEPTANCE_TIMEOUT_EXCEPTION_ENABLED='false')
        for key,file in [('OPS_SERVICE_TOKEN_FILE','ops-token'),('MA_ACTOR_KEY_FILE','actor-key'),('MA_ENVELOPE_KEY_FILE','envelope-key'),
                         ('OPS_EXECUTOR_TOKEN_FILE','executor-token'),('OPS_EXECUTOR_GRANT_KEY_FILE','executor-grant-key'),
                         ('OPS_EXECUTOR_WRITE_KEY_FILE','executor-write-key'),('SEC_SERVICE_TOKEN_FILE','sec-service-token'),
                         ('SEC_REQUEST_KEY_FILE','sec-request-key'),('SEC_RESULT_KEY_FILE','sec-result-key')]: env[key]=str(private/file)
        def envfile(name,values): (private/name).write_text('\n'.join(k+'='+v for k,v in values.items())+'\n',encoding='utf-8')
        envfile('local.env',env)
        tests=dict(env,OPS_DATABASE_URL=dsn('sec_m3_regression_test'),OPS_REDIS_URL=redis(6),OPS_REDIS_PREFIX='ma:sec-m3-regression:',MA_DRILL_ACCEPTANCE_ONLY='false',OPS_WRITE_ENABLED='true')
        envfile('m3-test.env',dict(tests,OPS_WRITE_ENABLED='false',MA_DRILL_ACCEPTANCE_ONLY='false'))
        tests.update(OPS_INSPECTION_ENABLED='false',OPS_DATABASE_URL=dsn('sec_m2_regression_test'),OPS_BACKEND_URL='http://127.0.0.1:18104',OPS_SECURITY_URL='http://127.0.0.1:8100',OPS_SUPERVISOR_URL='http://127.0.0.1:18096',OPS_EXECUTOR_URL='http://127.0.0.1:8102')
        envfile('sec-test.env',tests)
        legacy=dict(tests,OPS_DATABASE_URL=dsn('ma_m3_ops_regression_test'),OPS_SECURITY_ENABLED='false',OPS_BACKEND_URL='http://127.0.0.1:18113',OPS_WRITE_ENABLED='false',OPS_INFRA_ENABLED='false')
        envfile('ops-test.env',legacy)
        rag=dict(env,RAG_DATABASE_URL=dsn('sec_m3_rag_regression_test'),MA_DRILL_ACCEPTANCE_ONLY='false')
        for key in ('MA_ACTOR_KEY_FILE','MA_ENVELOPE_KEY_FILE','OPS_SERVICE_TOKEN_FILE'): rag.pop(key,None)
        envfile('rag-test.env',rag)
        envfile('business-test.env',{'NATURAL_TEST_POSTGRES_DSN':dsn('ma_m3_business_test')+'?sslmode=disable','MA_BUSINESS_TEST_REDIS_URL':redis(8),'V2M4_TEST_REDIS_URL':redis(8)})
        (private/'redis.conf').write_text('bind 0.0.0.0\nprotected-mode yes\nrequirepass '+rp+'\nsave ""\nappendonly no\n',encoding='utf-8')
        common={'restart':'no','read_only':True,'cap_drop':['ALL'],'security_opt':['no-new-privileges:true'],'labels':{'powersystem.milestone':'SEC-M3'}}
        compose={'name':project,'services':{
            'postgres':{'image':'postgres:17.6','ports':['127.0.0.1:15434:5432'],'environment':{'POSTGRES_USER':'sec_owner','POSTGRES_PASSWORD':password},'volumes':['pg-data:/var/lib/postgresql/data']},
            'redis':{'image':'redis:7.4.5-alpine','ports':['127.0.0.1:16379:6379'],'volumes':[str(private/'redis.conf')+':/usr/local/etc/redis/redis.conf:ro'],'command':['redis-server','/usr/local/etc/redis/redis.conf']},
            'drill':{**common,'networks':['drill-only'],'build':{'context':str(ROOT/'stages/09-ops-agent'),'dockerfile':'deploy/drill.Dockerfile'}},
            'broker':{**common,'networks':['control'],'ports':['127.0.0.1:8113:8090'],'build':{'context':str(ROOT/'stages/09-ops-agent'),'dockerfile':'deploy/drill-broker.Dockerfile'},'volumes':[str(private/'broker')+':/private:ro','/var/run/docker.sock:/var/run/docker.sock','broker-state:/state']}},
            'networks':{'drill-only':{'internal':True},'control':{}},'volumes':{'pg-data':{},'broker-state':{}}}
        write(private/'compose.json',compose)
    elif args.phase == 'databases':
        import asyncpg
        password=(private/'pg-password').read_text().strip()
        db=await asyncpg.connect(host='127.0.0.1',port=15434,user='sec_owner',password=password,database='postgres')
        for name in ('sec_m3_acceptance_test','sec_m3_regression_test','sec_m3_rag_acceptance_test','sec_m3_rag_regression_test','ma_m3_business_test','ma_m2_business_test','ma_m3_ops_regression_test','sec_m1_regression_test','sec_m2_regression_test'):
            if not await db.fetchval('SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=$1)',name):
                await db.execute('CREATE DATABASE '+name+' OWNER sec_owner');await db.execute('REVOKE CONNECT ON DATABASE '+name+' FROM PUBLIC')
        await db.close()
        env=read_env(private/'business-test.env');env['NATURAL_TEST_POSTGRES_DSN']=env['NATURAL_TEST_POSTGRES_DSN'].replace('/ma_m3_business_test','/ma_m2_business_test')
        (private/'business-test.env').write_text('\n'.join(k+'='+v for k,v in env.items())+'\n',encoding='utf-8')
        write(private/'m1-regression.json',{'host':'127.0.0.1','port':15434,'user':'sec_owner','password_file':str(private/'pg-password'),'regression_database':'sec_m1_regression_test','purpose':'SEC-M3 isolated regression'})
    elif args.phase == 'drill':
        if (private/'executor.json').exists(): raise ValueError('new_drill_batch_required')
        sys_path=str(ROOT/'stages/09-ops-agent');__import__('sys').path.insert(0,sys_path)
        from ops_contract import digest
        cid=docker('compose','-f',str(private/'compose.json'),'ps','-q','drill')
        info=json.loads(docker('inspect',cid))[0]
        if info['State'].get('Health',{}).get('Status')!='healthy': raise ValueError('drill_not_ready')
        if info['Config']['Labels'].get('com.docker.compose.project')!=project: raise ValueError('drill_not_owned')
        stamp=datetime.now(timezone.utc).isoformat()
        def asset(aid,kind,parent,capabilities):
            return dict(asset_id=aid,kind=kind,display_name='本机SEC-M3隔离演练容器' if parent else '本机SEC-M3演练环境',
                project_scope='powersystem',parent_id=parent,enabled=True,criticality='drill',environment='acceptance',version=1,
                verified_at=stamp,connection_ref='local-drill',identity_hash=digest({'project':project,'id':cid if parent else project}),capabilities=capabilities)
        assets=[asset('sec-m3-local-host','host',None,[]),asset('sec-m3-drill','container','sec-m3-local-host',['docker.inspect_container'])]
        registry={'assets':assets};write(private/'registry.json',registry)
        policy={'project':project,'milestone':'SEC-M3','network':info['HostConfig']['NetworkMode'],'tool':'docker.restart_drill_container','version':'1.0','grace_seconds':10,'tool_timeout_seconds':15,'asset_id':'sec-m3-drill','criticality':'drill','environment':'acceptance'}
        bp=private/'broker';bp.mkdir()
        for src,dst in [('executor-write-key','write-key'),('executor-grant-key','read-key')]: (bp/dst).write_bytes((private/src).read_bytes())
        broker={'asset_id':'sec-m3-drill','container_id':info['Id'],'image_digest':info['Image'],'registry':registry,'policy':policy,
            'write_enabled':False,'window_until':0,'window_file':'/private/write-window.json','write_key_file':'/private/write-key','read_key_file':'/private/read-key','state_file':'/state/attempts.sqlite'}
        write(bp/'write-window.json',{'write_enabled':False,'window_until':0});write(bp/'broker.json',broker)
        write(private/'executor.json',{'token_file':str(private/'executor-token'),'grant_key_file':str(private/'executor-grant-key'),
            'write_key_file':str(private/'executor-write-key'),'registry_file':str(private/'registry.json'),'callback_url':'http://127.0.0.1:8111',
            'hosts':{},'ssh_executable':'C:/Windows/System32/OpenSSH/ssh.exe','drill_url':'http://127.0.0.1:8113','security_required':True})
        write(run/'drill.json',{'project':project,'container_id':info['Id'],'image_digest':info['Image'],'registry_hash':digest(assets),'policy_version':digest(policy),'write_enabled':False})
    print(json.dumps({'prepared':args.phase,'batch':run.name,'project':project}))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--run',required=True);p.add_argument('--phase',choices=('config','databases','drill'),required=True)
    asyncio.run(main(p.parse_args()))
