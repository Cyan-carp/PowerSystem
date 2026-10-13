"""Admin-only import of the adapter's verified catalog. JWT/signatures remain in memory."""
import argparse
import asyncio
import json
from pathlib import Path
import httpx


def read_env(path):
    return dict(line.split('=',1) for line in Path(path).read_text(encoding='utf-8-sig').splitlines() if '=' in line and not line.startswith('#'))


async def run(args):
    cfg=read_env(args.env)
    if cfg.get('POSTGRES_DB')!='ma_m2_business_test' or cfg.get('OPS_BACKEND_URL')!='http://127.0.0.1:18082':raise ValueError('dedicated_m2_import_required')
    token=Path(cfg['OPS_SERVICE_TOKEN_FILE']).read_text().strip()
    adapter_token=Path(cfg['OPS_EXECUTOR_TOKEN_FILE']).read_text().strip()
    password=Path(cfg['MA_M2_ADMIN_PASSWORD_FILE']).read_text().strip()
    async with httpx.AsyncClient(trust_env=False,follow_redirects=False,timeout=20) as client:
        r=await client.post(cfg['OPS_BACKEND_URL']+'/api/v1/auth/login',json={'username':'ma_m2_admin','password':password});r.raise_for_status()
        jwt=r.json()['data']['token']
        headers={'Authorization':'Bearer '+jwt,'X-PowerSystem-Token':token}
        actor=await client.get(cfg['OPS_BACKEND_URL']+'/internal/ma/v1/actor',headers=headers);actor.raise_for_status()
        headers['X-MA-Actor']=actor.json()['data']['context']
        registry=await client.get(cfg['OPS_EXECUTOR_URL']+'/internal/ma-exec/v1/registry',headers={'X-MA-Executor-Token':adapter_token});registry.raise_for_status()
        r=await client.post(cfg['OPS_URL']+'/internal/ma/v1/assets/import',headers=headers,json={'registry_hash':registry.json()['registry_hash']});r.raise_for_status()
        if args.output.exists():raise ValueError('new_import_evidence_required')
        args.output.write_text(json.dumps(r.json(),ensure_ascii=False,indent=2),encoding='utf-8')
        print('Verified asset catalog imported through current admin authentication; no credential output.')


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--env',required=True);p.add_argument('--output',type=Path,required=True)
    asyncio.run(run(p.parse_args()))
