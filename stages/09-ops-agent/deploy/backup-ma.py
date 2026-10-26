"""Encrypted archive input only; exact active M4 configuration, no stdout secrets."""
import argparse,hashlib,json,os,shutil,stat,subprocess,tomllib
from pathlib import Path,PurePosixPath
ROOT=Path('/opt/powersystem')
HOST_PROJECT_ROOT=Path('/opt/powersystem')
# Deployment control tools the new VM needs after a cold copy. migration-data.py
# is new in this migration round; the rest already exist on the source host.
HOST_CONTROL_TOOLS=(
    HOST_PROJECT_ROOT/'stages/04-frontend/deploy/snapshot-production-queues.py',
    HOST_PROJECT_ROOT/'stages/04-frontend/deploy/snapshot_queues.py',
    HOST_PROJECT_ROOT/'stages/04-frontend/deploy/backup.sh',
    HOST_PROJECT_ROOT/'stages/04-frontend/deploy/restore-verify.sh',
    HOST_PROJECT_ROOT/'stages/04-frontend/deploy/backup-release-assets.py',
    HOST_PROJECT_ROOT/'stages/04-frontend/deploy/verify-release-backup-assets.py',
    HOST_PROJECT_ROOT/'stages/04-frontend/deploy/production-stack.py',
    HOST_PROJECT_ROOT/'stages/04-frontend/deploy/host-profile.py',
    HOST_PROJECT_ROOT/'stages/04-frontend/deploy/migration-vm.py',
    HOST_PROJECT_ROOT/'stages/04-frontend/deploy/migration-data.py',
    HOST_PROJECT_ROOT/'stages/04-frontend/deploy/source-build.py',
    HOST_PROJECT_ROOT/'stages/09-ops-agent/deploy/backup-ma.py',
    HOST_PROJECT_ROOT/'stages/09-ops-agent/deploy/restore-ma.py',
    HOST_PROJECT_ROOT/'stages/09-ops-agent/deploy/ma-release.py',
    HOST_PROJECT_ROOT/'stages/09-ops-agent/deploy/observe-ma.py',
)
# PowerSystem-only collector, its fixed entry points, sudo rule, backup units.
HOST_SINGLE_FILES=(
    Path('/usr/local/libexec/powersystem-ops-read'),
    Path('/usr/local/libexec/powersystem-ops-ssh-entry'),
    Path('/etc/sudoers.d/powersystem-ops-read'),
    Path('/var/lib/powersystem-ops-read/.ssh/authorized_keys'),
    Path('/etc/systemd/system/powersystem-backup.service'),
    Path('/etc/systemd/system/powersystem-backup.timer'),
)
HOST_TREES=(
    Path('/etc/powersystem-ops-read'),
    Path('/opt/powersystem-ops-read'),
)
# New VM only: the dedicated web-only FRP client. The shared /opt/frp and
# /etc/powersystem-edge client configs stay out of this archive.
WEB_FRP_CONFIG=Path('/etc/powersystem-web-frp')
WEB_FRP_BINARIES=Path('/opt/powersystem-web-frp')
WEB_FRP_UNITS=(
    Path('/etc/systemd/system/powersystem-web-primary-frpc.service'),
    Path('/etc/systemd/system/powersystem-web-secondary-frpc.service'),
)
WEB_FRP_REFERENCE_ROOTS=(Path('/etc/powersystem-edge'),Path('/etc/powersystem-web-frp'))
# Never part of recovery material: PVE config, whole-machine identity, whole
# SSH key trees and backup passphrases.
HOST_FORBIDDEN_ROOTS=(Path('/etc/pve'),Path('/root/.ssh'))
HOST_FORBIDDEN_NAMES=('machine-id','backup-passphrase')


def _host_logical(value):
    """Host paths stay POSIX-rooted even when tests run on Windows."""
    logical=PurePosixPath(Path(value).as_posix() if not isinstance(value,str) else value)
    if not logical.is_absolute() or '..' in logical.parts:raise ValueError('host_asset_absolute_path_required')
    return logical


def _host_under(logical,root):
    return _host_logical(logical).is_relative_to(_host_logical(root))


def host_path_forbidden(logical):
    logical=_host_logical(logical)
    if any(_host_under(logical,item) for item in HOST_FORBIDDEN_ROOTS):
        return True
    name=logical.name.lower()
    if name in HOST_FORBIDDEN_NAMES or 'passphrase' in name:
        return True
    return name.startswith('id_') and not name.endswith('.pub')


def _host_source(root,logical):
    return Path(root).joinpath(*_host_logical(logical).parts[1:])


def _host_capture_file(source,logical,ma,assets,seen):
    logical=_host_logical(logical)
    if host_path_forbidden(logical):raise ValueError('host_asset_forbidden:'+str(logical))
    if source.is_symlink() or not source.is_file():raise ValueError('host_asset_regular_file_required:'+str(logical))
    relative=Path('host-runtime')/Path(*logical.parts[1:])
    key=relative.as_posix()
    data=source.read_bytes();digest=hashlib.sha256(data).hexdigest()
    if key in seen:
        if seen[key]!=digest:raise ValueError('host_asset_capture_drift')
        return
    target=ma/relative
    target.parent.mkdir(parents=True,mode=0o700,exist_ok=True)
    target.write_bytes(data)
    if hashlib.sha256(target.read_bytes()).hexdigest()!=digest:raise ValueError('host_asset_capture_drift')
    assets.append({'source':str(source),'logical_path':str(logical),'archive':key,
                   'sha256':digest,'bytes':len(data),'mode':stat.S_IMODE(source.stat().st_mode)})
    seen[key]=digest


def _host_capture_tree(source,logical,ma,assets,symlinks,seen):
    logical=_host_logical(logical)
    if host_path_forbidden(logical):raise ValueError('host_asset_forbidden:'+str(logical))
    if source.is_symlink() or not source.is_dir():raise ValueError('host_asset_directory_required:'+str(logical))
    for directory,names,files in os.walk(source,followlinks=False):
        names.sort();base=Path(directory)
        for name in list(names):
            path=base/name
            if path.is_symlink():
                names.remove(name)
                symlinks.append({'source':str(path),'logical_path':str(logical/PurePosixPath(path.relative_to(source).as_posix())),
                                 'target':os.readlink(path)})
        for name in sorted(files):
            path=base/name;child=logical/PurePosixPath(path.relative_to(source).as_posix())
            if path.is_symlink():
                symlinks.append({'source':str(path),'logical_path':str(child),'target':os.readlink(path)})
                continue
            _host_capture_file(path,child,ma,assets,seen)


def web_frp_references(document):
    """Explicit credential paths only; never the shared FRP client config."""
    references=[]
    token=document.get('auth',{}).get('tokenSource',{})
    if token.get('type') not in (None,'file'):raise ValueError('web_frp_token_source_not_file')
    token_path=token.get('file',{}).get('path')
    if token_path:references.append(('auth.tokenSource.file.path',token_path))
    tls=document.get('transport',{}).get('tls',{})
    for key in ('certFile','keyFile','trustedCaFile'):
        if tls.get(key):references.append(('transport.tls.'+key,tls[key]))
    return references


def capture_host_assets(ma,root=Path('/')):
    """Archive host-side recovery material; restore only, never auto-activate.

    ``root`` exists so tests can exercise the exact whitelist against a
    temporary tree. Every captured byte stays inside ``ma/host-runtime`` and is
    covered by the outer SHA256SUMS of the encrypted backup.
    """
    ma=Path(ma);root=Path(root)
    assets=[];symlinks=[];seen={}
    for logical in HOST_CONTROL_TOOLS+HOST_SINGLE_FILES:
        source=_host_source(root,logical)
        if not source.is_file():raise ValueError('host_asset_missing:'+str(logical))
        _host_capture_file(source,logical,ma,assets,seen)
    for logical in HOST_TREES:
        _host_capture_tree(_host_source(root,logical),logical,ma,assets,symlinks,seen)
    web_frp=None
    config_dir=_host_source(root,WEB_FRP_CONFIG)
    if config_dir.is_dir():
        _host_capture_tree(config_dir,WEB_FRP_CONFIG,ma,assets,symlinks,seen)
        for logical in WEB_FRP_UNITS:
            source=_host_source(root,logical)
            if not source.is_file():raise ValueError('host_asset_missing:'+str(logical))
            _host_capture_file(source,logical,ma,assets,seen)
        binaries=_host_source(root,WEB_FRP_BINARIES)
        _host_capture_tree(binaries,WEB_FRP_BINARIES,ma,assets,symlinks,seen)
        if sum(1 for item in assets if _host_under(item['logical_path'],WEB_FRP_BINARIES))<2:
            raise ValueError('web_frp_binaries_missing')
        references={};configs=[]
        for path in sorted(config_dir.rglob('*.toml')):
            if path.is_symlink() or not path.is_file():raise ValueError('web_frp_config_symlink_forbidden')
            configs.append(str(_host_logical(WEB_FRP_CONFIG/PurePosixPath(path.relative_to(config_dir).as_posix()))))
            for key,value in web_frp_references(tomllib.loads(path.read_text())):
                try:logical=_host_logical(value)
                except ValueError:raise ValueError('web_frp_reference_outside_owned_scope:'+key) from None
                if not any(logical.is_relative_to(_host_logical(item)) for item in WEB_FRP_REFERENCE_ROOTS):
                    raise ValueError('web_frp_reference_outside_owned_scope:'+key)
                source=_host_source(root,logical)
                if not source.is_file():raise ValueError('web_frp_reference_missing:'+key)
                _host_capture_file(source,logical,ma,assets,seen)
                references[key]=str(logical)
        if not configs:raise ValueError('web_frp_config_missing')
        web_frp={'configs':configs,'references':references,'activation_forbidden':True}
    route_config=Path('/etc/systemd/network/10-netplan-management.network.d/50-powersystem-cloud-routes.conf')
    if _host_source(root,route_config).is_file():
        _host_capture_file(_host_source(root,route_config),route_config,ma,assets,seen)
    return {'restore_material_only':True,'restore_activation_forbidden':True,
            'host_registration_evidence_only':True,'credentials_encrypted_archive_only':True,
            'root':str(root),'assets':assets,'symlinks':symlinks,'web_frp':web_frp,
            'counts':{'files':len(assets),'bytes':sum(item['bytes'] for item in assets),
                      'symlinks':len(symlinks)}}
RELEASE_SERVICES=('api','agent-rag','ops-agent','security-agent')
RELEASE_REQUIRED_TARGETS={'api':'/run/powersystem-fleet','agent-rag':'/run/ma-supervisor',
                          'ops-agent':'/run/ma','security-agent':'/run/v4-security-action-key'}
RELEASE_EXISTING_SECRETS={
    ('api','/run/secrets/agent_service_token','/etc/powersystem/agent-service-token'):'config/agent/agent-service-token',
    ('api','/run/secrets/feishu_business_token','/etc/powersystem/business-notify-token'):'config/business-notify-token',
}


def _release_owned(value,runtime):
    """Only already-mounted private sources under ROOT/runtime, never /etc."""
    path=Path(value)
    if not path.is_absolute():raise ValueError('actual_release_private_source_required')
    try:resolved=path.resolve(strict=True)
    except (OSError,RuntimeError):raise ValueError('actual_release_private_source_required') from None
    if resolved!=path or path.is_symlink() or not resolved.is_relative_to(runtime):
        raise ValueError('actual_release_private_source_required')
    return path


def _release_capture_file(source,ma,files,role,runtime):
    relative=Path('release-runtime/runtime')/source.relative_to(runtime)
    key=relative.as_posix()
    data=source.read_bytes();digest=hashlib.sha256(data).hexdigest()
    if key in files:
        if files[key]['sha256']!=digest:raise ValueError('release_private_capture_drift')
        if role not in files[key]['services']:files[key]['services'].append(role)
        return key
    target=ma/relative;target.parent.mkdir(parents=True,mode=0o700,exist_ok=True)
    target.write_bytes(data)
    if hashlib.sha256(target.read_bytes()).hexdigest()!=digest:raise ValueError('release_private_capture_drift')
    files[key]={'source':str(source),'archive':key,'sha256':digest,'bytes':len(data),
                'mode':stat.S_IMODE(source.stat().st_mode),'services':[role]}
    return key


def _release_capture_tree(source,ma,files,role,runtime):
    keys=[]
    for directory,names,filenames in os.walk(source,followlinks=False):
        base=Path(directory);names.sort()
        for name in names:
            if (base/name).is_symlink():raise ValueError('release_private_symlink_forbidden')
        for name in sorted(filenames):
            path=base/name
            if path.is_symlink():raise ValueError('release_private_symlink_forbidden')
            if not path.is_file():raise ValueError('release_private_regular_file_required')
            keys.append(_release_capture_file(path,ma,files,role,runtime))
    return keys


def capture_release_configuration(ma,rows):
    """Capture the four services' actual readonly /run private mounts.

    Security's live action key is mounted outside MA_SECURITY_PRIVATE_DIR, so
    the old static tree copy cannot see it. Only bind mounts already handed to
    the services under /run are archived; never /etc or arbitrary host files.
    The identity machine-id arrives through the mounted runtime identity
    directory and stays evidence-only.
    """
    ma=Path(ma);runtime=(ROOT/'runtime').resolve()
    if not isinstance(rows,dict) or set(rows)!=set(RELEASE_SERVICES):
        raise ValueError('exactly_four_release_services_required')
    files={};services={}
    for role in RELEASE_SERVICES:
        info=rows[role];labels=info['Config']['Labels']
        if labels.get('com.docker.compose.project')!='powersystem-stage4' or labels.get('com.docker.compose.service')!=role:
            raise ValueError('owned_release_service_required:'+role)
        mounts=[];captured=[]
        for mount in info.get('Mounts',[]):
            target=mount.get('Destination','')
            if not target.startswith('/run/'):continue
            if mount.get('Type')!='bind' or mount.get('RW'):
                raise ValueError('readonly_release_private_bind_required:'+role)
            existing=RELEASE_EXISTING_SECRETS.get((role,target,mount.get('Source')))
            if existing:
                original=Path(mount['Source']);archived=ma.parent/existing
                if original.is_symlink() or not archived.is_file() or hashlib.sha256(original.read_bytes()).digest()!=hashlib.sha256(archived.read_bytes()).digest():
                    raise ValueError('existing_release_secret_backup_mismatch')
                mounts.append({'source':str(original),'target':target,'covered_by':existing})
                continue
            source=_release_owned(mount.get('Source'),runtime)
            mounts.append({'source':str(source),'target':target})
            if source.is_dir():captured.extend(_release_capture_tree(source,ma,files,role,runtime))
            elif source.is_file():captured.append(_release_capture_file(source,ma,files,role,runtime))
            else:raise ValueError('release_private_regular_file_required')
        if not any(item['target']==RELEASE_REQUIRED_TARGETS[role] or
                   item['target'].startswith(RELEASE_REQUIRED_TARGETS[role]+'/') for item in mounts):
            raise ValueError('required_release_mount_missing:'+role)
        if not captured:raise ValueError('release_private_source_required:'+role)
        services[role]={'mounts':mounts,'source':[item['source'] for item in mounts],
                        'target':[item['target'] for item in mounts],'files':sorted(set(captured))}
    return {'services':services,'files':files,'restore_execution_forbidden':True,
            'counts':{'files':len(files),'bytes':sum(item['bytes'] for item in files.values())}}
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
    run([__import__('sys').executable,str(ROOT/'stages/04-frontend/deploy/host-profile.py'),'backup'])
    if not out.is_relative_to(Path('/var/backups/powersystem')):raise ValueError('private_backup_staging_required')
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
    release_rows={s:json.loads(run(['docker','inspect','powersystem-stage4-'+s+'-1']))[0] for s in RELEASE_SERVICES if s!='agent-rag'}
    release_rows['agent-rag']=rag_info
    release_configuration=capture_release_configuration(ma,release_rows)
    shutil.copyfile(envpath,ma/'compose.env')
    shutil.copyfile(ROOT/'stages/09-ops-agent/deploy/production.compose.yaml',ma/'production.compose.yaml')
    for name in ('registry.json','installation.json'):shutil.copyfile(Path('/etc/powersystem-ops-read')/name,ma/('collector-'+name))
    shutil.copytree(ROOT/'stages/10-security-agent/policy',ma/'policy')
    audit=Path(env['MA_OPS_AUDIT_DIR']).resolve()
    if not audit.is_relative_to(ROOT/'runtime'):raise ValueError('owned_audit_required')
    if audit.exists():shutil.copytree(audit,ma/'audit')
    images={}
    for s in ('ops-agent','security-agent','ops-executor','api','frontend','agent-rag'):
        images[s]=release_rows[s]['Image'] if s in release_rows else json.loads(run(['docker','inspect','powersystem-stage4-'+s+'-1']))[0]['Image']
    host_profile=Path('/etc/powersystem/host-profiles.json')
    shutil.copyfile(host_profile,ma/'host-profiles.json')
    host_runtime=capture_host_assets(ma)
    metadata={'version':'ma-backup/v1','release':release.name,'counts':counts,'images':images,'runtime_profile':'m4-server-readonly','restore_execution_forbidden':True,'actual_private_configuration':configs,'actual_rag_configuration':rag_configuration,'actual_release_configuration':release_configuration,
              'host_registration':{'archive':'host-profiles.json','sha256':hashlib.sha256(host_profile.read_bytes()).hexdigest(),'restore_activation_forbidden':True},
              'host_runtime':host_runtime}
    (ma/'manifest.json').write_text(json.dumps(metadata,indent=2))
    for path in ma.rglob('*'):path.chmod(0o700 if path.is_dir() else 0o600)
    print(json.dumps({'ma_backup_saved':True,'tables':len(counts),'tasks':counts['ma_tasks']}))
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);main(p.parse_args())
