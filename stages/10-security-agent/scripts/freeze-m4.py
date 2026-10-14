"""Freeze public runtime source, dependencies, asset and model configurations."""
import argparse,hashlib,json,subprocess
from pathlib import Path
from datetime import datetime,timezone
ROOT=Path(__file__).resolve().parents[3]
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def main(a):
    run=(ROOT/a.run).resolve();out=run/a.output_name
    import re
    if not re.fullmatch(r'runtime-manifest-[a-z0-9-]+\.json',a.output_name) or not re.fullmatch(r'ma-m4-[a-z0-9-]+',a.candidate):raise ValueError('explicit_candidate_and_manifest_required')
    if not run.is_relative_to(ROOT/'artifacts/stage10-security-agent') or not run.name.startswith('sec-m4-') or out.exists():raise ValueError('new_owned_freeze_required')
    files={}
    for folder in ('stages/09-ops-agent/ops_agent','stages/09-ops-agent/ops_executor','stages/09-ops-agent/collector','stages/09-ops-agent/drill','stages/10-security-agent/security_agent','stages/10-security-agent/policy','stages/08-agent-rag/rag_agent','stages/02-backend','stages/04-frontend/src'):
        for p in (ROOT/folder).rglob('*'):
            if p.is_file() and '__pycache__' not in p.parts and '.pytest_cache' not in p.parts and p.suffix not in ('.pyc','.exe') and 'tests' not in p.parts and not p.name.endswith(('_test.go','.test.ts')):files[str(p.relative_to(ROOT))]=sha(p)
    for folder in ('stages/09-ops-agent/deploy','stages/08-agent-rag/deploy'):
        for p in (ROOT/folder).glob('*.sql'):files[str(p.relative_to(ROOT))]=sha(p)
    for name in ('stages/09-ops-agent/ops_contract.py','stages/09-ops-agent/ops_write_contract.py','stages/10-security-agent/review_contract.py','stages/09-ops-agent/requirements.txt','stages/10-security-agent/requirements.txt','stages/08-agent-rag/requirements.txt','stages/04-frontend/package-lock.json','stages/09-ops-agent/deploy/production.compose.yaml','stages/09-ops-agent/deploy/security.Dockerfile','stages/09-ops-agent/deploy/security.Dockerfile.dockerignore','stages/09-ops-agent/deploy/executor.Dockerfile','stages/09-ops-agent/deploy/executor.Dockerfile.dockerignore','stages/10-security-agent/Dockerfile','stages/10-security-agent/.dockerignore'):
        files[name]=sha(ROOT/name)
    images={}
    for name in ('ops-agent','security-agent','ops-executor'):
        tag='powersystem-ma-'+name+':'+a.candidate;r=subprocess.run(['docker','image','inspect',tag],capture_output=True,check=True);i=json.loads(r.stdout)[0];images[name]={'tag':tag,'id':i['Id'],'user':i['Config']['User']}
    env=dict(l.split('=',1) for l in (run/'private/local.env').read_text().splitlines() if '=' in l)
    value={'milestone':'SEC-M4','candidate':a.candidate,'created_at':datetime.now(timezone.utc).isoformat(),'files':dict(sorted(files.items())),'images':images,'asset_registry_sha256':sha(run/'private/registry.json'),'models':{k:env[k] for k in ('OPS_MODEL','OPS_LLM_URL','SEC_MODEL','SEC_LLM_URL','SEC_MODEL_TIMEOUT_SECONDS','SEC_REVIEW_TTL_SECONDS','AGENT_LLM_MODEL','AGENT_LLM_BASE_URL')},'profiles':{'m4-local':{'policy_version':'sec-m4-policy/v2','scope_version':'sec-m4-local-integration/v2'},'m4-server-readonly':{'policy_version':'sec-m4-policy/v5','scope_version':'sec-m4-project-readonly/v5'}}}
    out.write_text(json.dumps(value,ensure_ascii=False,indent=2));print(json.dumps({'runtime_manifest_sha256':sha(out),'files':len(files),'images':len(images)}))
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--run',required=True);p.add_argument('--candidate',default='ma-m4-20261014-r4');p.add_argument('--output-name',default='runtime-manifest-r4.json');main(p.parse_args())
