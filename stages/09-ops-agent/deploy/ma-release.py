"""R730XD-only M4 release; exact services, retained data, no public ports."""
import argparse,hashlib,json,os,re,shutil,subprocess,tarfile,time
from pathlib import Path
ROOT=Path('/opt/powersystem')
BASE_SERVICES=('api','frontend','agent-rag')
NEW_SERVICES=('ops-agent','security-agent','ops-executor')
BASE_FILES=('stages/04-frontend/deploy/compose.yaml','stages/07-agent/deploy/agent.compose.yaml','stages/07-agent/deploy/search.compose.yaml','stages/08-agent-rag/deploy/production.compose.yaml')
OVERLAY='stages/09-ops-agent/deploy/production.compose.yaml'
COLLECTOR_AUTH=Path('/var/lib/powersystem-ops-read/.ssh/authorized_keys')
ALLOWED=('stages/02-backend/','stages/04-frontend/','stages/08-agent-rag/','stages/09-ops-agent/','stages/10-security-agent/','notes/02-工程规范边界与验收治理/','notes/03-平台使用运维与二次开发手册/','notes/01-项目调研规划与开发建设记录/02-第二版-智能体升级/','规范/','手册/','内容/02-第二版-智能体升级/')
KNOWLEDGE_FILES=('notes/05-设备知识设计素材与历史归档资产/设备知识/SUN2000-100KTL-M2-仿真参考.md','资产/设备知识/SUN2000-100KTL-M2-仿真参考.md')

def command(argv,**kw):
    r=subprocess.run(argv,capture_output=True,**kw)
    if r.returncode:raise RuntimeError('release_command_failed:'+Path(argv[0]).name)
    return r.stdout
def inspect(service):
    return json.loads(command(['docker','inspect','powersystem-stage4-'+service+'-1']))[0]
def write(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8');path.chmod(0o600)
def envread(path):
    return dict(l.split('=',1) for l in path.read_text(encoding='utf-8').splitlines() if '=' in l and not l.lstrip().startswith('#'))
def envwrite(path,value):
    path.write_text(''.join(k+'='+str(v)+'\n' for k,v in value.items()),encoding='utf-8');path.chmod(0o600)
def archive_members(path):
    with tarfile.open(path) as ar:
        for m in ar.getmembers():
            if not m.isfile() or m.name.startswith('/') or '..' in Path(m.name).parts or any(x in Path(m.name).parts for x in ('.git','.env','private','artifacts','node_modules','__pycache__')):raise ValueError('invalid_release_member')
            if not (m.name in ('api-server','README.md','.env.example',*KNOWLEDGE_FILES) or m.name.startswith(('dist/',*ALLOWED))):raise ValueError('unreviewed_release_member')
            yield m,ar.extractfile(m).read()
def compose(release,baseline=False):
    env=release/'private/compose-before.env' if baseline else release/'private/compose.env'
    args=['docker','compose','--env-file',str(env)]
    for name in BASE_FILES:args+=['-f',str(ROOT/name)]
    if not baseline:args+=['-f',str(ROOT/OVERLAY)]
    args+=['-f',str(release/('baseline-images.yaml' if baseline else 'candidate-images.yaml')),'--profile','agent']
    return args

def prepare(r):
    if (r/'baseline.json').exists():raise ValueError('baseline_exists')
    if any(subprocess.run(['docker','inspect','powersystem-stage4-'+s+'-1'],capture_output=True).returncode==0 for s in NEW_SERVICES):raise ValueError('new_services_already_exist')
    rows={s:inspect(s) for s in BASE_SERVICES}
    envpath=Path(rows['agent-rag']['Config']['Labels']['com.docker.compose.project.environment_file'])
    if not envpath.is_relative_to(ROOT/'runtime') or not envpath.is_file():raise ValueError('actual_compose_environment_required')
    private=r/'private';private.mkdir(mode=0o700,exist_ok=True)
    shutil.copyfile(envpath,private/'compose-before.env');(private/'compose-before.env').chmod(0o600)
    backup=[];new=[]
    for member,data in archive_members(r/'candidate.tar.gz'):
        if member.name=='api-server' or member.name.startswith('dist/'):continue
        target=ROOT/member.name
        if target.is_file():backup.append(member.name)
        else:new.append(member.name)
    for name in (*BASE_FILES,OVERLAY,'stages/04-frontend/deploy/backup.sh','stages/04-frontend/deploy/restore-verify.sh'):
        if (ROOT/name).is_file() and name not in backup:backup.append(name)
    with tarfile.open(private/'source-before.tar.gz','w:gz') as ar:
        for name in sorted(backup):ar.add(ROOT/name,arcname=name,recursive=False)
    collector=Path('/etc/powersystem-ops-read')
    if not (collector/'installation.json').is_file() or json.loads((collector/'installation.json').read_text()).get('owner')!='powersystem-ma-m2':raise ValueError('owned_collector_required')
    for name in ('registry.json','installation.json'):
        shutil.copyfile(collector/name,private/('collector-before-'+name));(private/('collector-before-'+name)).chmod(0o600)
    images={}
    for s,row in rows.items():
        tag='powersystem-ma-'+s+':baseline-'+r.name
        command(['docker','tag',row['Image'],tag]);images[s]=tag
    (r/'baseline-images.yaml').write_text('services:\n'+''.join('  '+s+':\n    image: '+images[s]+'\n' for s in BASE_SERVICES))
    write(r/'baseline.json',{'services':{s:{'image':v['Image'],'started_at':v['State']['StartedAt'],'restarts':v['RestartCount']} for s,v in rows.items()},'new_paths':new,'source_paths':backup,'environment_source':str(envpath),'project':'powersystem-stage4'})
    with (private/'rag-before.dump').open('wb') as out:
        p=subprocess.run(['docker','exec','powersystem-stage4-postgres-1','pg_dump','-U','powersystem','-d','powersystem_agent_rag','-Fc'],stdout=out,stderr=subprocess.DEVNULL)
        if p.returncode:raise ValueError('baseline_dump_failed')
    (private/'rag-before.dump').chmod(0o600)

def build(r):
    if not (r/'baseline.json').is_file():raise ValueError('prepare_required')
    candidate=r/'candidate';candidate.mkdir(exist_ok=False)
    for m,data in archive_members(r/'candidate.tar.gz'):
        target=candidate/m.name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
    images={}
    for service,dockerfile in {
        'api':'FROM powersystem-ma-api:baseline-'+r.name+'\nCOPY --chmod=755 api-server /app/server\n',
        'frontend':'FROM powersystem-ma-frontend:baseline-'+r.name+'\nCOPY dist /usr/share/nginx/html\n',
        'agent-rag':'FROM powersystem-ma-agent-rag:baseline-'+r.name+'\nUSER root\nCOPY stages/08-agent-rag/rag_agent /app/rag_agent\nCOPY stages/08-agent-rag/deploy /app/deploy\nUSER 10002:10002\n',
    }.items():
        path=candidate/('Dockerfile.'+service);path.write_text(dockerfile)
        tag='powersystem-ma-'+service+':'+r.name
        command(['docker','build','--network=none','--pull=false','-f',str(path),'-t',tag,str(candidate)]);images[service]=tag
    for service in NEW_SERVICES:
        tag='powersystem-ma-'+service+':'+r.name
        command(['docker','image','inspect',tag]);images[service]=tag
    (r/'candidate-images.yaml').write_text('services:\n'+''.join('  '+s+':\n    image: '+tag+'\n' for s,tag in images.items()))
    write(r/'candidate-images.json',{s:json.loads(command(['docker','image','inspect',tag]))[0]['Id'] for s,tag in images.items()})

def migrate(r):
    # Only the dedicated new task database; business/RAG schemas are untouched.
    password=(r/'private/ops/db-password').read_text().strip()
    if not re.fullmatch('[A-Za-z0-9_-]{32,}',password):raise ValueError('invalid_private_database_password')
    probe=command(['docker','exec','powersystem-stage4-postgres-1','psql','-U','powersystem','-d','postgres','-Atqc',"SELECT count(*) FROM pg_database WHERE datname='ma_ops'"]).decode().strip()
    if probe!='0' and not (r/'database-created.json').is_file():raise ValueError('existing_unowned_ma_ops')
    if probe=='0':
        sql="CREATE ROLE ma_ops_runtime LOGIN PASSWORD '"+password+"' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;\nCREATE DATABASE ma_ops OWNER ma_ops_runtime;\nREVOKE CONNECT ON DATABASE ma_ops FROM PUBLIC;\n"
        command(['docker','exec','-i','powersystem-stage4-postgres-1','psql','-U','powersystem','-d','postgres','-v','ON_ERROR_STOP=1','-q'],input=sql.encode())
        write(r/'database-created.json',{'database':'ma_ops','role':'ma_ops_runtime','preserve_on_rollback':True})
    sql=''.join((r/'candidate/stages/09-ops-agent/deploy'/name).read_text() for name in ('001_tasks.sql','002_assets.sql','004_approvals.sql','005_security_reviews.sql','006_security_gates.sql','007_runtime_context.sql'))
    sql='SET ROLE ma_ops_runtime;\n'+sql
    command(['docker','exec','-i','powersystem-stage4-postgres-1','psql','-U','powersystem','-d','ma_ops','-v','ON_ERROR_STOP=1','-q'],input=sql.encode())

def source_target(name):
    target=ROOT/name
    if target.is_symlink() or not target.resolve().is_relative_to(ROOT.resolve()):raise ValueError('source_target_outside_project')
    return target

def collector_authorization(r,enabled):
    public=(r/'private/executor/collector-ed25519.pub').read_text().strip()
    if not re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/=]+(?: [^\r\n]+)?',public):raise ValueError('restricted_collector_public_key_required')
    line='restrict,command="/usr/bin/python3 -I /usr/local/libexec/powersystem-ops-ssh-entry" '+public
    lines=COLLECTOR_AUTH.read_text().splitlines()
    if enabled and line not in lines:lines.append(line)
    if not enabled:lines=[value for value in lines if value!=line]
    COLLECTOR_AUTH.write_text('\n'.join(lines)+'\n');COLLECTOR_AUTH.chmod(0o644)

def restore_source(r):
    with tarfile.open(r/'private/source-before.tar.gz') as ar:
        for member in ar.getmembers():
            if not member.isfile() or member.name.startswith('/') or '..' in Path(member.name).parts:raise ValueError('invalid_baseline_member')
            target=source_target(member.name);target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(ar.extractfile(member).read());target.chmod(member.mode)
    baseline=json.loads((r/'baseline.json').read_text())
    quarantined=[]
    quarantine=r/'private/new-source-on-rollback'
    if quarantine.exists():quarantine=r/('private/new-source-on-rollback-'+str(time.time_ns()))
    for name in baseline['new_paths']:
        if not name.startswith(ALLOWED) and name not in ('README.md','.env.example',*KNOWLEDGE_FILES):raise ValueError('invalid_new_source_path')
        target=source_target(name)
        if target.is_file():
            saved=quarantine/name
            if saved.exists():raise ValueError('rollback_source_evidence_exists')
            saved.parent.mkdir(parents=True,exist_ok=True);shutil.move(str(target),str(saved));quarantined.append(name)
    marker=r/'source-restored.json'
    if marker.exists():shutil.copyfile(marker,r/('source-restored-history-'+str(time.time_ns())+'.json'))
    write(marker,{'baseline_files_restored':True,'new_files_quarantined':quarantined,'quarantine_directory':str(quarantine)})

def activate(r):
    if not (r/'sec-m4-signed.json').is_file() or not (r/'database-created.json').is_file():raise ValueError('local_signoff_and_migration_required')
    signed=json.loads((r/'sec-m4-signed.json').read_text())
    candidate=json.loads((r/'candidate-manifest.json').read_text())
    if not re.fullmatch('[a-f0-9]{64}',str(signed.get('runtime_manifest_sha256',''))) or signed.get('milestone')!='SEC-M4' or signed.get('signed_off') is not True or signed.get('runtime_manifest_sha256')!=candidate.get('runtime_manifest_sha256'):
        raise ValueError('same_candidate_signoff_required')
    if candidate.get('archive_sha256')!=hashlib.sha256((r/'candidate.tar.gz').read_bytes()).hexdigest():raise ValueError('candidate_archive_changed')
    for m,data in archive_members(r/'candidate.tar.gz'):
        if m.name=='api-server' or m.name.startswith('dist/'):continue
        target=source_target(m.name);target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data);target.chmod(0o750 if target.suffix=='.sh' else 0o644)
    collector_authorization(r,True)
    command(compose(r)+['up','-d','--no-deps','--no-build','--pull','never','--force-recreate','--wait',*NEW_SERVICES,*BASE_SERVICES])

def rollback(r):
    # Close admission before draining. Existing facts and DB/audit remain retained.
    command(compose(r)+['stop','api','agent-rag'])
    until=time.monotonic()+300
    while True:
        active=command(['docker','exec','powersystem-stage4-postgres-1','psql','-U','powersystem','-d','ma_ops','-Atqc',"SELECT count(*) FROM ma_tasks WHERE status IN ('queued','planning','executing','awaiting_approval')"]).decode().strip()
        if active=='0':break
        if time.monotonic()>=until:raise ValueError('inflight_tasks_not_drained')
        time.sleep(2)
    command(compose(r)+['stop',*NEW_SERVICES])
    collector_authorization(r,False)
    restore_source(r)
    command(compose(r,True)+['up','-d','--no-deps','--no-build','--pull','never','--force-recreate','--wait',*BASE_SERVICES])
    for name in ('registry.json','installation.json'):shutil.copyfile(r/'private'/('collector-before-'+name),Path('/etc/powersystem-ops-read')/name)
    marker=r/'rollback-completed.json'
    if marker.exists():shutil.copyfile(marker,r/('rollback-completed-history-'+str(time.time_ns())+'.json'))
    write(marker,{'data_preserved':True,'new_services_stopped':True,'baseline_images':{s:inspect(s)['Image'] for s in BASE_SERVICES}})

def reload_executor_registry():
    # Keep the registered Executor identity stable while loading the new registry.
    executor_before=inspect('ops-executor')['Id']
    command(['docker','restart','powersystem-stage4-ops-executor-1'])
    until=time.monotonic()+90
    while True:
        executor_after=inspect('ops-executor')
        if executor_after['Id']!=executor_before:raise ValueError('executor_identity_changed_during_registry_reload')
        if executor_after['State'].get('Health',{}).get('Status')=='healthy':break
        if time.monotonic()>=until:raise ValueError('executor_registry_reload_not_ready')
        time.sleep(1)

def advanced_asset_version(current,retained,changed):
    if not changed:return current
    # Only database-committed versions are history. A failed import can leave
    # a newer provisional host registry, which must not consume another version.
    return retained+1

def refresh_assets(r):
    sys=__import__('sys');sys.path.insert(0,str(ROOT/'stages/09-ops-agent'))
    from ops_contract import digest,validate_catalog
    path=Path('/etc/powersystem-ops-read/registry.json');registry=json.loads(path.read_text());old=digest(registry['assets'])
    existing={a['asset_id'] for a in registry['assets']}
    retained=json.loads(command(['docker','exec','powersystem-stage4-postgres-1','psql','-U','powersystem','-d','ma_ops','-Atqc',"SELECT coalesce(json_object_agg(asset_id,version),'{}'::json)::text FROM ma_assets"]))
    template=next(a for a in registry['assets'] if a['kind']=='container' and a['enabled'] and registry['private_assets'][a['asset_id']]['identity']['service']=='api')
    prefix=template['asset_id'].removesuffix('-api')
    for service in NEW_SERVICES:
        aid=prefix+'-'+service
        if aid in existing and not next(a for a in registry['assets'] if a['asset_id']==aid)['enabled']:
            raise ValueError('active_host_service_asset_is_disabled:'+service)
        if aid not in existing:
            asset={**template,'asset_id':aid,'display_name':service+' 项目容器','version':0,'criticality':'support'}
            registry['assets'].append(asset)
            registry['private_assets'][aid]={'identity':{'container_id':'pending','image_id':'pending','project':'powersystem-stage4','service':service}}
    for asset in registry['assets']:
        if asset['kind']!='container' or not asset['enabled']:continue
        p=registry['private_assets'][asset['asset_id']];identity=p['identity']
        info=inspect(identity['service'])
        labels=info['Config']['Labels']
        actual={'container_id':info['Id'],'image_id':info['Image'],'project':labels['com.docker.compose.project'],'service':labels['com.docker.compose.service']}
        if actual['project']!='powersystem-stage4':raise ValueError('asset_scope_mismatch')
        if actual!=identity:
            p['identity']=actual;asset['identity_hash']=digest(actual);asset['version']=advanced_asset_version(asset['version'],retained.get(asset['asset_id'],0),True);asset['verified_at']=__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()
    validate_catalog(registry['assets']);write(path,registry)
    metadata=Path('/etc/powersystem-ops-read/installation.json');value=json.loads(metadata.read_text());value['registry_hash']=digest(registry['assets']);write(metadata,value)
    public={'assets':registry['assets']};write(r/'private/executor/registry.json',public);os.chown(r/'private/executor/registry.json',10005,10005)
    write(r/'asset-refresh.json',{'old_registry_hash':old,'registry_hash':digest(public['assets']),'assets':len(public['assets'])})
    reload_executor_registry()
    # Explicit host release maintenance; no counterfeit user JWT or approvals.
    code="""import asyncio,json,sys
from ops_agent.config import Config
from ops_agent.store import Store
async def main():
 cfg=Config.load()
 assert cfg.runtime_profile=='m4-server-readonly' and not cfg.write_enabled
 store=await Store.open(cfg)
 try:
  assert await store.pool.fetchval("SELECT count(*) FROM ma_tasks WHERE status IN ('queued','planning','executing','awaiting_approval')")==0
  v=json.load(sys.stdin)
  print(json.dumps(await store.import_assets(v['assets'],v['registry_hash'],0)))
 finally:await store.pool.close()
asyncio.run(main())
"""
    imported=command(['docker','exec','-i','powersystem-stage4-ops-agent-1','python','-c',code],input=json.dumps({'assets':registry['assets'],'registry_hash':digest(registry['assets'])}).encode())
    write(r/('asset-import-'+str(time.time_ns())+'.json'),{'source':'owner-authorized host release program','imported_by':0,'result':json.loads(imported),'task_or_approval_created':False})


def main(a):
    command([__import__('sys').executable,str(ROOT/'stages/04-frontend/deploy/host-profile.py'),'registry'])
    r=a.release.resolve()
    if not r.is_relative_to(ROOT/'runtime') or not re.fullmatch('ma-m4-[a-z0-9-]{1,65}',r.name):raise ValueError('new_private_release_required')
    r.mkdir(mode=0o700,parents=True,exist_ok=True)
    globals()[a.phase](r)
    print(json.dumps({'phase':a.phase,'release':r.name,'done':True}))
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('phase',choices=('prepare','build','migrate','activate','rollback','refresh_assets'));p.add_argument('--release',type=Path,required=True);main(p.parse_args())
