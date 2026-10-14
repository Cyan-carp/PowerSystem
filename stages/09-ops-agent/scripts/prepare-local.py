"""Create private MA test databases/config; never alter business tables or existing secrets."""
import argparse
import asyncio
import os
import secrets
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit
import asyncpg


def read_env(path):
    return dict(line.split("=", 1) for line in Path(path).read_text(encoding="utf-8-sig").splitlines() if "=" in line and not line.startswith("#"))


async def main(args):
    root = Path(__file__).resolve().parents[3]; run = (root / args.run).resolve(); private = run / "private"
    if not private.is_relative_to(root / "artifacts/stage9-ops-agent"): raise ValueError("invalid_run")
    env = read_env(root / ".env"); env.update(read_env(args.source))
    # Admin access is confined to creating these new logical test databases and their dedicated owner.
    base = read_env(root / ".env")
    db = await asyncpg.connect(host="127.0.0.1", port=5432, user=base.get("POSTGRES_USER", "powersystem"), password=base["POSTGRES_PASSWORD"], database="postgres")
    password = (private / "ops-db-password").read_text().strip()
    role = "ma_m1_test"
    if not await db.fetchval("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=$1)", role):
        await db.execute("CREATE ROLE ma_m1_test LOGIN PASSWORD '"+password+"' NOSUPERUSER NOCREATEDB NOCREATEROLE")
    for name in ("ma_m1_ops_test", "ma_m1_ops_regression_test", "ma_m1_rag_test", "ma_m1_rag_regression_test", "ma_m1_business_test"):
        if not await db.fetchval("SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=$1)", name):
            await db.execute("CREATE DATABASE " + name + " OWNER ma_m1_test")
    runtime_secret=private/'runtime-db-password'
    if not runtime_secret.exists():runtime_secret.write_text(secrets.token_urlsafe(36),encoding='utf-8')
    runtime_password=runtime_secret.read_text().strip()
    if not await db.fetchval("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='ma_ops_runtime')"):
        await db.execute("CREATE ROLE ma_ops_runtime LOGIN PASSWORD '"+runtime_password+"' NOSUPERUSER NOCREATEDB NOCREATEROLE")
    if not await db.fetchval("SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname='ma_ops')"):
        await db.execute("CREATE DATABASE ma_ops OWNER ma_ops_runtime")
        await db.execute('REVOKE CONNECT ON DATABASE ma_ops FROM PUBLIC')
        await db.execute('GRANT CONNECT ON DATABASE ma_ops TO ma_ops_runtime')
    await db.close()
    def dsn(name): return "postgresql://"+role+":"+quote(password,safe="")+"@127.0.0.1:5432/"+name
    # Keep the existing business source configuration and model untouched; use a separate local proxy port.
    env.update(HTTP_ADDR="127.0.0.1:18081", AGENT_CHAT_URL="http://127.0.0.1:18093", AGENT_BACKEND_URL="http://127.0.0.1:18081",
               OPS_URL="http://127.0.0.1:8094", OPS_BACKEND_URL="http://127.0.0.1:18081", OPS_SUPERVISOR_URL="http://127.0.0.1:18093",
               OPS_SERVICE_TOKEN_FILE=str(private/"ops-token"), MA_ACTOR_KEY_FILE=str(private/"actor-key"), MA_ENVELOPE_KEY_FILE=str(private/"envelope-key"),
               OPS_LLM_API_KEY_FILE=str(private/"ops-llm-key"), OPS_LLM_URL="https://hgapi.dieqiyun.top/v1", OPS_MODEL="deepseek-v4-flash-0731",
               OPS_ENABLED="true", OPS_WRITE_ENABLED="false", OPS_DATABASE_URL=dsn("ma_m1_ops_test"),
               OPS_REDIS_URL=env["AGENT_REDIS_URL"], OPS_REDIS_PREFIX="ma:m1-test:", RAG_DATABASE_URL=dsn("ma_m1_rag_test"),
               RAG_AUDIT_DIR=str(run/"supervisor-audit"), OPS_AUDIT_DIR=str(run/"ops-audit"))
    # Store all inherited private values here; stdout never contains them.
    for name in ("AGENT_SERVICE_TOKEN_FILE", "AGENT_LLM_API_KEY_FILE", "AGENT_SEARCH_KEY_FILE", "RAG_EMBEDDING_PATH"):
        value=env.get(name)
        if value and not Path(value).is_absolute(): env[name]=str(root/value)
    (private/"local.env").write_text("\n".join(k+"="+v for k,v in env.items())+"\n",encoding="utf-8")
    test=dict(env);test["OPS_ENABLED"]="false";test["RAG_AUDIT_DIR"]=str(run/"test-audit")
    test["RAG_DATABASE_URL"]=dsn("ma_m1_rag_regression_test")
    test["AGENT_REDIS_URL"]="redis://127.0.0.1:16379/0"
    test["OPS_REDIS_URL"]="redis://127.0.0.1:16379/0"
    # Legacy BND fixtures provide mocked trusted identity; signature-specific tests use the MA suite.
    test.pop("MA_ACTOR_KEY_FILE",None);test.pop("MA_ENVELOPE_KEY_FILE",None);test.pop("OPS_SERVICE_TOKEN_FILE",None)
    (private/"rag-test.env").write_text("\n".join(k+"="+v for k,v in test.items())+"\n",encoding="utf-8")
    ops_test=dict(env);ops_test["OPS_DATABASE_URL"]=dsn("ma_m1_ops_regression_test");ops_test["OPS_REDIS_PREFIX"]="ma:m1-regression:"
    ops_test["AGENT_REDIS_URL"]="redis://127.0.0.1:16379/0"
    ops_test["OPS_REDIS_URL"]="redis://127.0.0.1:16379/0"
    (private/"ops-test.env").write_text("\n".join(k+"="+v for k,v in ops_test.items())+"\n",encoding="utf-8")
    (private/'business-test.env').write_text('NATURAL_TEST_POSTGRES_DSN='+dsn('ma_m1_business_test')+'?sslmode=disable\nMA_BUSINESS_TEST_REDIS_URL=redis://127.0.0.1:16379\nV2M4_TEST_REDIS_URL=redis://127.0.0.1:16379\n',encoding='utf-8')
    runtime=dict(env);runtime.update(OPS_DATABASE_URL='postgresql://ma_ops_runtime:'+quote(runtime_password,safe='')+'@127.0.0.1:5432/ma_ops',OPS_ENABLED='false',OPS_WRITE_ENABLED='false',OPS_REDIS_PREFIX='ma:runtime:')
    (private/'runtime-disabled.env').write_text('\n'.join(k+'='+v for k,v in runtime.items())+'\n',encoding='utf-8')
    runtime_db=await asyncpg.connect(runtime['OPS_DATABASE_URL'])
    await runtime_db.execute((root/'stages/09-ops-agent/deploy/001_tasks.sql').read_text(encoding='utf-8'))
    await runtime_db.close()
    print("Dedicated ma_m1_ops_test / ma_m1_rag_test prepared; private configuration written.")


if __name__ == "__main__":
    p=argparse.ArgumentParser();p.add_argument("--source",required=True);p.add_argument("--run",required=True)
    asyncio.run(main(p.parse_args()))
