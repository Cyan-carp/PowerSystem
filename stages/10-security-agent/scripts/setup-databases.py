"""Create new dedicated SEC test databases; never changes the original databases."""
import argparse
import asyncio
import json
import secrets
from pathlib import Path
from urllib.parse import quote

import asyncpg


async def main(run):
    root=Path(__file__).resolve().parents[3]; run=(root/run).resolve()
    if not run.is_relative_to(root/'artifacts/stage10-security-agent'): raise ValueError('invalid_sec_run')
    private=run/'private'; password=(private/'pg-password').read_text().strip()
    conn=await asyncpg.connect(host='127.0.0.1',port=15432,user='sec_owner',password=password,database='postgres')
    try:
        for name in ('sec_m1_acceptance_test','sec_m1_regression_test','ma_m3_ops_regression_test','sec_m1_rag_regression_test'):
            if not await conn.fetchval('SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=$1)', name):
                await conn.execute('CREATE DATABASE '+name+' OWNER sec_owner')
                await conn.execute('REVOKE CONNECT ON DATABASE '+name+' FROM PUBLIC')
    finally: await conn.close()
    creds={name:str(private/name) for name in ('sec-service-token','sec-request-key','sec-result-key')}
    for name in ('ma-fixture-llm-key','ma-fixture-executor-token','ma-fixture-grant-key','ma-fixture-write-key'):
        target=private/name
        if not target.exists():target.write_text(secrets.token_urlsafe(36),encoding='utf-8')
    config={'host':'127.0.0.1','port':15432,'user':'sec_owner','password_file':str(private/'pg-password'),
            'acceptance_database':'sec_m1_acceptance_test','regression_database':'sec_m1_regression_test'}
    (private/'databases.json').write_text(json.dumps(config),encoding='utf-8')
    dsn=lambda db:'postgresql://sec_owner:'+quote(password,safe='')+'@127.0.0.1:15432/'+db
    # Existing MA fixtures demand these exact regression database names. Separate SEC tables live elsewhere.
    env={'OPS_DATABASE_URL':dsn('ma_m3_ops_regression_test'),
         'OPS_REDIS_URL':'redis://:'+quote((private/'redis-password').read_text().strip(),safe='')+'@127.0.0.1:16376/6',
         'OPS_REDIS_PREFIX':'ma:sec-m1-regression:', 'OPS_ENABLED':'false','OPS_INFRA_ENABLED':'false','OPS_WRITE_ENABLED':'false',
         'OPS_SERVICE_TOKEN_FILE':creds['sec-service-token'],'MA_ACTOR_KEY_FILE':creds['sec-request-key'],
         'MA_ENVELOPE_KEY_FILE':creds['sec-result-key'], 'OPS_BACKEND_URL':'http://127.0.0.1:18103',
         'OPS_LLM_API_KEY_FILE':str(private/'ma-fixture-llm-key'),
         'OPS_EXECUTOR_TOKEN_FILE':str(private/'ma-fixture-executor-token'),
         'OPS_EXECUTOR_GRANT_KEY_FILE':str(private/'ma-fixture-grant-key'),
         'OPS_EXECUTOR_WRITE_KEY_FILE':str(private/'ma-fixture-write-key'),
         'OPS_AUDIT_DIR':str(run/'ma-regression-audit')}
    (private/'ops-test.env').write_text('\n'.join(k+'='+v for k,v in env.items())+'\n',encoding='utf-8')
    rag={'RAG_DATABASE_URL':dsn('sec_m1_rag_regression_test'),
         'AGENT_REDIS_URL':env['OPS_REDIS_URL'],'AGENT_SERVICE_TOKEN_FILE':creds['sec-service-token'],
         'AGENT_LLM_MODEL':'fixture','AGENT_LLM_BASE_URL':'http://127.0.0.1:18103/v1',
         'RAG_AUDIT_DIR':str(run/'rag-regression-audit')}
    (private/'rag-test.env').write_text('\n'.join(k+'='+v for k,v in rag.items())+'\n',encoding='utf-8')
    print(json.dumps({'isolated_databases_ready':True}))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--run',required=True)
    asyncio.run(main(parser.parse_args().run))
