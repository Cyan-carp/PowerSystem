"""Prepare new logical test databases and private MA-M2 config; no business/old-credential updates."""
import argparse
import asyncio
import json
import secrets
from pathlib import Path
from urllib.parse import quote
import asyncpg


def read_env(path):
    return dict(line.split('=',1) for line in Path(path).read_text(encoding='utf-8-sig').splitlines() if '=' in line and not line.startswith('#'))


async def main(args):
    root=Path(__file__).resolve().parents[3];run=(root/args.run).resolve();private=run/'private'
    if not run.is_relative_to(root/'artifacts/stage9-ops-agent'): raise ValueError('invalid_run')
    private.mkdir(parents=True,exist_ok=True)
    inherited=read_env(args.source);base=read_env(root/'.env')
    for name in ('ops-token','actor-key','envelope-key','executor-token','executor-grant-key','ops-db-password','admin-password','operator-password','redis-password'):
        path=private/name
        if not path.exists(): path.write_text(secrets.token_urlsafe(36),encoding='utf-8')
    llm=private/'ops-llm-key'
    if not llm.exists(): llm.write_bytes(Path(inherited['OPS_LLM_API_KEY_FILE']).read_bytes())
    password=(private/'ops-db-password').read_text().strip()
    db=await asyncpg.connect(host='127.0.0.1',port=5432,user=base.get('POSTGRES_USER','powersystem'),password=base['POSTGRES_PASSWORD'],database='postgres')
    role='ma_m2_test'
    if not await db.fetchval('SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=$1)',role):
        await db.execute("CREATE ROLE ma_m2_test LOGIN PASSWORD '"+password+"' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT")
    names=('ma_m2_ops_test','ma_m2_ops_regression_test','ma_m2_rag_test','ma_m2_rag_regression_test','ma_m2_business_test')
    for name in names:
        if not await db.fetchval('SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=$1)',name):
            await db.execute('CREATE DATABASE '+name+' OWNER '+role)
            await db.execute('REVOKE CONNECT ON DATABASE '+name+' FROM PUBLIC')
    await db.close()
    def dsn(name): return 'postgresql://'+role+':'+quote(password,safe='')+'@127.0.0.1:5432/'+name
    redis_password=(private/'redis-password').read_text().strip()
    def redis_url(db):return 'redis://:'+quote(redis_password,safe='')+'@127.0.0.1:16378/'+str(db)
    (private/'redis.conf').write_text('bind 0.0.0.0\nprotected-mode yes\nport 6379\nrequirepass '+redis_password+'\nsave ""\nappendonly no\n',encoding='utf-8')
    env=dict(inherited)
    env.update(OPS_DATABASE_URL=dsn('ma_m2_ops_test'),RAG_DATABASE_URL=dsn('ma_m2_rag_test'),
        OPS_REDIS_URL=redis_url(4),AGENT_REDIS_URL=redis_url(5),OPS_REDIS_PREFIX='ma:m2-acceptance:',
        OPS_SERVICE_TOKEN_FILE=str(private/'ops-token'),MA_ACTOR_KEY_FILE=str(private/'actor-key'),
        MA_ENVELOPE_KEY_FILE=str(private/'envelope-key'),OPS_LLM_API_KEY_FILE=str(llm),
        OPS_INFRA_ENABLED='true',OPS_EXECUTOR_URL='http://127.0.0.1:8095',
        OPS_EXECUTOR_TOKEN_FILE=str(private/'executor-token'),OPS_EXECUTOR_GRANT_KEY_FILE=str(private/'executor-grant-key'),
        OPS_EXECUTOR_CONFIG_FILE=str(private/'executor.json'),OPS_AUDIT_DIR=str(run/'ops-audit'),RAG_AUDIT_DIR=str(run/'supervisor-audit'),
        POSTGRES_HOST='127.0.0.1',POSTGRES_PORT='5432',POSTGRES_USER=role,POSTGRES_PASSWORD=password,POSTGRES_DB='ma_m2_business_test',
        REDIS_ADDR='127.0.0.1:16378',REDIS_PASSWORD=redis_password,HTTP_ADDR='127.0.0.1:18082',AGENT_CHAT_URL='http://127.0.0.1:18094',
        AGENT_BACKEND_URL='http://127.0.0.1:18082',OPS_BACKEND_URL='http://127.0.0.1:18082',OPS_SUPERVISOR_URL='http://127.0.0.1:18094',
        OPS_URL='http://127.0.0.1:8096',MA_M2_ADMIN_PASSWORD_FILE=str(private/'admin-password'),MA_M2_OPERATOR_PASSWORD_FILE=str(private/'operator-password'),
        NATURAL_TEST_USERNAME='ma_m2_operator',NATURAL_TEST_PASSWORD=(private/'operator-password').read_text().strip())
    def write(name,values): (private/name).write_text('\n'.join(k+'='+v for k,v in values.items())+'\n',encoding='utf-8')
    write('local.env',env)
    regression=dict(env);regression.update(OPS_DATABASE_URL=dsn('ma_m2_ops_regression_test'),OPS_REDIS_URL=redis_url(6),OPS_REDIS_PREFIX='ma:m2-regression:',OPS_INFRA_ENABLED='false')
    write('ops-test.env',regression)
    rag=dict(env);rag.update(RAG_DATABASE_URL=dsn('ma_m2_rag_regression_test'),AGENT_REDIS_URL=redis_url(7),RAG_AUDIT_DIR=str(run/'rag-test-audit'))
    for key in ('MA_ACTOR_KEY_FILE','MA_ENVELOPE_KEY_FILE','OPS_SERVICE_TOKEN_FILE'): rag.pop(key,None)
    write('rag-test.env',rag)
    write('business-test.env',{'NATURAL_TEST_POSTGRES_DSN':dsn('ma_m2_business_test')+'?sslmode=disable','MA_BUSINESS_TEST_REDIS_URL':redis_url(8),'V2M4_TEST_REDIS_URL':redis_url(8)})
    print('MA-M2 new dedicated test databases and private configurations prepared; no business records changed.')


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--run',required=True);p.add_argument('--source',required=True)
    asyncio.run(main(p.parse_args()))
