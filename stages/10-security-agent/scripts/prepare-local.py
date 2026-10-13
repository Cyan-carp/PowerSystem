"""Prepare fresh SEC-only private files and isolated Compose configuration."""
import argparse
import json
import secrets
import re
from pathlib import Path


def prepare(run):
    root = Path(__file__).resolve().parents[3]
    run = (root / run).resolve()
    if not run.is_relative_to(root / 'artifacts/stage10-security-agent'):
        raise ValueError('invalid_sec_run')
    if not re.fullmatch(r'sec-m1-[a-z0-9-]{1,60}',run.name): raise ValueError('invalid_sec_batch_name')
    private = run / 'private'; private.mkdir(parents=True, exist_ok=True)
    for name in ('sec-service-token','sec-request-key','sec-result-key','pg-password','redis-password'):
        target = private / name
        if not target.exists(): target.write_text(secrets.token_urlsafe(36), encoding='utf-8')
    env = {'SEC_ENABLED': 'true', 'SEC_MODEL': 'deepseek-v4-flash-0731', 'SEC_LLM_URL': 'https://hgapi.dieqiyun.top/v1'}
    for name, file in [('SEC_SERVICE_TOKEN_FILE','sec-service-token'),('SEC_REQUEST_KEY_FILE','sec-request-key'),
                       ('SEC_RESULT_KEY_FILE','sec-result-key'),('SEC_LLM_API_KEY_FILE','sec-llm-key')]:
        env[name] = str(private / file)
    target = private / 'local.env'
    if target.exists(): raise ValueError('sec_config_already_exists')
    target.write_text('\n'.join(k+'='+v for k,v in env.items())+'\n', encoding='utf-8')
    (private / 'redis.conf').write_text('bind 0.0.0.0\nprotected-mode yes\nrequirepass '+
        (private/'redis-password').read_text().strip()+'\nsave ""\nappendonly no\n', encoding='utf-8')
    # No private password in process arguments; PostgreSQL reads the mounted file.
    compose = {'name': 'powersystem-'+run.name, 'services': {
        'postgres': {'image':'postgres:17.6','ports':['127.0.0.1:15432:5432'],
            'environment': {'POSTGRES_USER':'sec_owner','POSTGRES_DB':'postgres','POSTGRES_PASSWORD_FILE':'/run/secrets/pg-password'},
            'volumes':[str(private/'pg-password')+':/run/secrets/pg-password:ro','sec-pg:/var/lib/postgresql/data']},
        'redis': {'image':'redis:7-alpine','ports':['127.0.0.1:16376:6379'],
            'command':['redis-server','/etc/redis/redis.conf'],
            'volumes':[str(private/'redis.conf')+':/etc/redis/redis.conf:ro']}}, 'volumes': {'sec-pg': {}}}
    (private/'compose.json').write_text(json.dumps(compose, indent=2), encoding='utf-8')
    print(json.dumps({'prepared':True,'run':run.name,'model':env['SEC_MODEL']}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--run', required=True)
    prepare(parser.parse_args().run)
