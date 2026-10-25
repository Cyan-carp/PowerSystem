"""Encrypted archive input only; exact active M4 configuration, no stdout secrets."""
import argparse,hashlib,json,os,shutil,subprocess
from pathlib import Path
ROOT=Path('/opt/powersystem')
TABLES=('ma_tasks','ma_task_steps','ma_tool_calls','ma_task_events','ma_outbox','ma_session_guards','ma_assets','ma_asset_versions','ma_approvals','ma_execution_attempts','ma_policy_versions','ma_security_reviews','ma_security_review_events','ma_security_gate_contexts')
def run(argv):
    r=subprocess.run(argv,capture_output=True)
    if r.returncode:raise RuntimeError('ma_backup_command_failed')
    return r.stdout

def capture_rag_configuration(ma,info):
    """RAG can be recreated while Ops keeps its older compose environment label.

    Capture actual RAG env and /run mounts separately; never infer its new key
    directory from the unchanged Ops container. This tree is encrypted input,
    not credentials to activate in a recovery application.
    """
    labels=info['Config']['Labels']
    if labels.get('com.docker.compose.project')!='powersystem-stage4' or labels.get('com.docker.compose.service')!='agent-rag':
        raise ValueError('owned_rag_service_required')
    runtime=(ROOT/'runtime').resolve()
    def owned(value):
        path=Path(value)
        if not path.is_absolute() or not path.exists() or path.resolve()!=path or not path.is_relative_to(runtime):
            raise ValueError('actual_rag_private_source_required')
        return path
    origin=owned(labels['com.docker.compose.project.environment_file'])
    env=dict((k.strip(),v.strip().strip("\"'")) for k,v in (line.split('=',1) for line in origin.read_text().splitlines() if '=' in line and not line.lstrip().startswith('#')))
    sources={origin}
    for key in ('RAG_ENV_FILE','MA_RAG_ENV_FILE'):
        sources.add(owned(env[key]))
    for mount in info['Mounts']:
        if mount['Destination'].startswith('/run/'):
            if mount['Type']!='bind' or mount['RW']:
                raise ValueError('readonly_rag_private_bind_required')
            sources.add(owned(mount['Source']))
    if not any(m['Destination']=='/run/ma-supervisor' for m in info['Mounts']):
        raise ValueError('actual_rag_supervisor_required')
    files={}
    for source in sorted(sources):
        candidates=sorted(source.rglob('*')) if source.is_dir() else [source]
        for path in candidates:
            if path.is_symlink():raise ValueError('rag_private_symlink_forbidden')
            if path.is_file():
                relative=Path('rag-runtime')/path.relative_to(ROOT)
                target=ma/relative
                if not target.exists():
                    target.parent.mkdir(parents=True,mode=0o700,exist_ok=True)
                    shutil.copyfile(path,target)
                digest=hashlib.sha256(path.read_bytes()).hexdigest()
                if hashlib.sha256(target.read_bytes()).hexdigest()!=digest:raise ValueError('rag_private_capture_drift')
                files[relative.as_posix()]={'source':str(path),'sha256':digest}
    return {'origin_environment':str(origin),'files':files,'restore_execution_forbidden':True}

def main(a):
    out=a.output.resolve()
    if os.uname().nodename!='R730XD' or not out.is_relative_to(Path('/var/backups/powersystem')):raise ValueError('private_backup_staging_required')
    info=json.loads(run(['docker','inspect','powersystem-stage4-ops-agent-1']))[0]
    labels=info['Config']['Labels']
    if labels.get('com.docker.compose.project')!='powersystem-stage4':raise ValueError('owned_ops_service_required')
    envpath=Path(labels['com.docker.compose.project.environment_file'])
    env=dict(l.split('=',1) for l in envpath.read_text().splitlines() if '=' in l and not l.lstrip().startswith('#'))
    release=envpath.parent.parent
    if not release.is_relative_to(ROOT/'runtime') or not release.name.startswith('ma-m4-'):raise ValueError('actual_ma_release_required')
    if run(['docker','exec','powersystem-stage4-postgres-1','psql','-U','powersystem','-d','ma_ops','-Atqc',"SELECT count(*) FROM ma_tasks WHERE status IN ('queued','planning','executing','awaiting_approval')"]).decode().strip()!='0':raise ValueError('drained_tasks_required_before_backup')
    ma=out/'ma';ma.mkdir(mode=0o700,exist_ok=False)
    with (ma/'ma_ops.dump').open('wb') as stream:
        r=subprocess.run(['docker','exec','powersystem-stage4-postgres-1','pg_dump','-U','powersystem','-d','ma_ops','-Fc'],stdout=stream,stderr=subprocess.DEVNULL)
        if r.returncode:raise ValueError('ma_dump_failed')
    counts={t:int(run(['docker','exec','powersystem-stage4-postgres-1','psql','-U','powersystem','-d','ma_ops','-Atqc','SELECT count(*) FROM '+t]).decode()) for t in TABLES}
    requests={}
    raw=json.loads(run(['docker','exec','powersystem-stage4-postgres-1','psql','-U','powersystem','-d','ma_ops','-Atqc',"SELECT coalesce(json_agg(json_build_object('task_id',task_id,'user_id',user_id,'status',status,'result',result)),'[]'::json)::text FROM ma_tasks"]))
    (ma/'task-baseline.json').write_text(json.dumps(raw,ensure_ascii=False,indent=2))
    for key in ('MA_PRIVATE_DIR','MA_SECURITY_PRIVATE_DIR','MA_EXECUTOR_PRIVATE_DIR','MA_SUPERVISOR_PRIVATE_DIR','MA_API_PRIVATE_DIR'):
        source=Path(env[key]).resolve()
        if not source.is_relative_to(release/'private'):raise ValueError('owned_config_source_required')
        shutil.copytree(source,ma/'private'/source.name)
    configs={}
    for key in ('MA_OPS_ENV_FILE','MA_SECURITY_ENV_FILE','MA_RAG_ENV_FILE'):
        source=Path(env[key]).resolve()
        if not source.is_relative_to(release/'private') or not source.is_file():raise ValueError('actual_runtime_config_required')
        target=ma/'config'/source.name;target.parent.mkdir(mode=0o700,exist_ok=True)
        shutil.copyfile(source,target)
        configs[key]={'source':str(source),'archive':'config/'+source.name,'sha256':hashlib.sha256(source.read_bytes()).hexdigest()}
    rag_info=json.loads(run(['docker','inspect','powersystem-stage4-agent-rag-1']))[0]
    rag_configuration=capture_rag_configuration(ma,rag_info)
    shutil.copyfile(envpath,ma/'compose.env')
    shutil.copyfile(ROOT/'stages/09-ops-agent/deploy/production.compose.yaml',ma/'production.compose.yaml')
    for name in ('registry.json','installation.json'):shutil.copyfile(Path('/etc/powersystem-ops-read')/name,ma/('collector-'+name))
    shutil.copytree(ROOT/'stages/10-security-agent/policy',ma/'policy')
    audit=Path(env['MA_OPS_AUDIT_DIR']).resolve()
    if not audit.is_relative_to(ROOT/'runtime'):raise ValueError('owned_audit_required')
    if audit.exists():shutil.copytree(audit,ma/'audit')
    for path in ma.rglob('*'):path.chmod(0o700 if path.is_dir() else 0o600)
    images={}
    for s in ('ops-agent','security-agent','ops-executor','api','frontend','agent-rag'):
        images[s]=json.loads(run(['docker','inspect','powersystem-stage4-'+s+'-1']))[0]['Image']
    metadata={'version':'ma-backup/v1','release':release.name,'counts':counts,'images':images,'runtime_profile':'m4-server-readonly','restore_execution_forbidden':True,'actual_private_configuration':configs,'actual_rag_configuration':rag_configuration}
    (ma/'manifest.json').write_text(json.dumps(metadata,indent=2));(ma/'manifest.json').chmod(0o600)
    print(json.dumps({'ma_backup_saved':True,'tables':len(counts),'tasks':counts['ma_tasks']}))
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);main(p.parse_args())
