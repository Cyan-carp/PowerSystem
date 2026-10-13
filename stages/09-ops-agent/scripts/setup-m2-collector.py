"""Install only the explicitly authorized MA-M2 collector/account/diagnostic functions.

Secrets travel over stdin of host-key-verified SSH, never argv/stdout. No business service restart.
"""
import argparse
import base64
import hashlib
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

REMOTE_PROBE=r'''
import hashlib,json,pathlib,subprocess
def run(args,input=None):return subprocess.check_output(args,input=input,text=True,timeout=12).strip()
project='powersystem-stage4'
names=run(['docker','ps','-a','--filter','label=com.docker.compose.project='+project,'--format','{{.Names}}']).splitlines()
containers=[]
for name in names:
    line=run(['docker','inspect','--format','{{.Id}}|{{.Image}}|{{index .Config.Labels "com.docker.compose.project"}}|{{index .Config.Labels "com.docker.compose.service"}}|{{json .NetworkSettings.Networks}}|{{.State.StartedAt}}|{{.RestartCount}}',name])
    cid,image,scope,service,networks,started,restarts=line.split('|',6)
    if scope!=project:raise RuntimeError('scope_mismatch')
    containers.append(dict(container_id=cid,image_id=image,project=scope,service=service,networks=list(json.loads(networks)),started_at=started,restart_count=int(restarts)))
units=[]
for unit in ('powersystem-edge-frpc.service','powersystem-backup.service'):
    fragment=run(['systemctl','show',unit,'--property=FragmentPath','--value'])
    if fragment and pathlib.Path(fragment).is_file():
        if not fragment.startswith('/etc/systemd/system/powersystem-'):raise RuntimeError('unit_not_project_owned')
        units.append(dict(unit=unit,fragment_hash=hashlib.sha256(pathlib.Path(fragment).read_bytes()).hexdigest()))
pg=next(c for c in containers if c['service']=='postgres')
roles=run(['docker','exec',pg['container_id'],'psql','-U','powersystem','-d','powersystem','-Atc',
          "SELECT rolname FROM pg_roles WHERE rolname IN ('ma_ops_diagnostics','ma_ops_monitor_owner')"])
meta=pathlib.Path('/etc/powersystem-ops-read/installation.json')
account=subprocess.run(['getent','passwd','powersystem-ops-read'],capture_output=True).returncode==0
owned=meta.exists() and json.loads(meta.read_text()).get('owner')=='powersystem-ma-m2'
if (account or roles) and not owned:raise RuntimeError('collector_account_or_role_already_exists_unowned')
print(json.dumps(dict(machine_hash=hashlib.sha256(pathlib.Path('/etc/machine-id').read_bytes().strip()).hexdigest(),
                     containers=containers,units=units,owned=owned,python=run(['python3','--version']))))
'''

REMOTE_INSTALL=r'''
import base64,hashlib,json,os,pathlib,subprocess,sys
payload=json.loads(base64.b64decode(PAYLOAD))
root=pathlib.Path('/opt/powersystem-ops-read');config=pathlib.Path('/etc/powersystem-ops-read')
def run(args,input=None):
    result=subprocess.run(args,input=input,capture_output=True,text=True,timeout=120)
    if result.returncode:raise RuntimeError('collector_setup_command_failed:'+pathlib.Path(args[0]).name)
    return result.stdout
for directory in (root,root/'code',root/'code/collector',root/'wheels',config):directory.mkdir(mode=0o700,parents=True,exist_ok=True)
meta=config/'installation.json'
if meta.exists() and json.loads(meta.read_text()).get('owner')!='powersystem-ma-m2':raise RuntimeError('existing_unowned_directory')
if not meta.exists():
    meta.write_text(json.dumps({'owner':'powersystem-ma-m2','status':'installing','created_objects':[]}));meta.chmod(0o600)
for name,content in payload['code'].items():
    if name not in ('ops_contract.py','collector/__init__.py','collector/broker.py'):raise RuntimeError('invalid_code_target')
    target=root/'code'/name;target.write_bytes(base64.b64decode(content));target.chmod(0o600)
for name,content in payload['wheels'].items():
    if '/' in name or not name.endswith('.whl'):raise RuntimeError('invalid_wheel')
    (root/'wheels'/name).write_bytes(base64.b64decode(content))
if not (root/'venv/bin/python').exists():run(['/usr/bin/python3','-m','venv',str(root/'venv')])
run([str(root/'venv/bin/python'),'-m','pip','install','--no-index','--find-links',str(root/'wheels'),'psycopg==3.2.13','psycopg-binary==3.2.13','typing_extensions==4.15.0'])
(config/'grant-key').write_text(payload['grant_key']);(config/'grant-key').chmod(0o600)
(config/'db-password').write_text(payload['db_password']);(config/'db-password').chmod(0o600)
(config/'registry.json').write_text(json.dumps(payload['registry'],ensure_ascii=False));(config/'registry.json').chmod(0o600)
pg=payload['postgres_id'];password=chr(39)+payload['db_password'].replace(chr(39),chr(39)*2)+chr(39)
sql="SELECT 'CREATE ROLE ma_ops_monitor_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE' WHERE NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='ma_ops_monitor_owner')\n\\gexec\n"
sql+="SELECT format('CREATE ROLE ma_ops_diagnostics LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT CONNECTION LIMIT 2', "+password+") WHERE NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='ma_ops_diagnostics')\n\\gexec\n"
sql+="GRANT pg_read_all_stats TO ma_ops_monitor_owner;\nALTER ROLE ma_ops_diagnostics SET default_transaction_read_only=on;\nALTER ROLE ma_ops_diagnostics SET statement_timeout='5s';\n"+payload['sql']
run(['docker','exec','-i',pg,'psql','-U','powersystem','-d','powersystem','-v','ON_ERROR_STOP=1','-q'],sql)
if subprocess.run(['getent','passwd','powersystem-ops-read'],capture_output=True).returncode:
    run(['/usr/sbin/useradd','--system','--create-home','--home-dir','/var/lib/powersystem-ops-read','--shell','/bin/sh','powersystem-ops-read'])
run(['/usr/sbin/usermod','--lock','powersystem-ops-read'])
libexec=pathlib.Path('/usr/local/libexec');libexec.mkdir(exist_ok=True)
broker=libexec/'powersystem-ops-read'
broker.write_text('#!/opt/powersystem-ops-read/venv/bin/python -I\nimport sys\nsys.path.insert(0,"/opt/powersystem-ops-read/code")\nfrom collector.broker import main\nmain()\n');broker.chmod(0o755)
entry=libexec/'powersystem-ops-ssh-entry';entry.write_text(payload['ssh_entry']);entry.chmod(0o755)
sudoers=pathlib.Path('/etc/sudoers.d/powersystem-ops-read')
sudoers.write_text('powersystem-ops-read ALL=(root) NOPASSWD: /usr/local/libexec/powersystem-ops-read ""\n');sudoers.chmod(0o440)
run(['/usr/sbin/visudo','-cf',str(sudoers)])
home=pathlib.Path('/var/lib/powersystem-ops-read');home.chmod(0o755);os.chown(home,0,0)
auth=home/'.ssh';auth.mkdir(exist_ok=True);auth.chmod(0o755);os.chown(auth,0,0)
key=auth/'authorized_keys';key.write_text('restrict,command="/usr/bin/python3 -I /usr/local/libexec/powersystem-ops-ssh-entry" '+payload['public_key'].strip()+'\n');key.chmod(0o644);os.chown(key,0,0)
metadata={'owner':'powersystem-ma-m2','registry_hash':payload['registry_hash'],'created_objects':['powersystem-ops-read','ma_ops_diagnostics','ma_ops_monitor_owner','ma_ops_monitor'],
          'code_hashes':{name:hashlib.sha256(base64.b64decode(content)).hexdigest() for name,content in payload['code'].items()}}
meta.write_text(json.dumps(metadata));meta.chmod(0o600)
print(json.dumps({'installed':True,'asset_count':len(payload['registry']['assets']),'registry_hash':payload['registry_hash'],
                  'write_tools_registered':False,'business_restarted':False,'metadata':metadata}))
'''


def main(args):
    from datetime import datetime,timezone
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    from ops_contract import TOOLS,digest,validate_catalog
    root=Path(__file__).resolve().parents[3];run=(root/args.run).resolve();private=run/'private'
    if not run.is_relative_to(root/'artifacts/stage9-ops-agent'):raise ValueError('invalid_run')
    ssh=Path(os.environ.get('SYSTEMROOT','C:/Windows'))/'System32/OpenSSH/ssh.exe'
    keygen=ssh.with_name('ssh-keygen.exe');admin_key=Path.home()/'.ssh/id_ed25519'
    endpoint='8.138.42.212';port=7070
    admin=[str(ssh),'-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','UpdateHostKeys=no','-o','ConnectTimeout=8',
           '-i',str(admin_key),'-p',str(port),'root@'+endpoint,'python3','-']
    attempt=run/'remote'/args.attempt
    if attempt.exists():raise ValueError('new_setup_attempt_required')
    attempt.mkdir()
    def remote(source,stage):
        result=subprocess.run(admin,input=source,capture_output=True,text=True,encoding='utf-8',timeout=240)
        if result.returncode:
            (attempt/('failure-'+stage+'.txt')).write_text('SSH/setup failed; exception type or exit code only. exit='+str(result.returncode),encoding='utf-8')
            # Preserve private diagnostics for local review; never print private payload/error text.
            (private/('setup-error-'+args.attempt+'.txt')).write_text(result.stderr,encoding='utf-8')
            raise RuntimeError('remote_'+stage+'_failed')
        return json.loads(result.stdout)
    probe=remote(REMOTE_PROBE,'probe')
    (attempt/'preflight.json').write_text(json.dumps(probe,ensure_ascii=False,indent=2),encoding='utf-8')
    restricted_key=private/'collector-ed25519'
    if not restricted_key.exists():
        result=subprocess.run([str(keygen),'-q','-t','ed25519','-N','','-f',str(restricted_key)],capture_output=True)
        if result.returncode:raise RuntimeError('key_generation_failed')
    known=subprocess.run([str(keygen),'-F','['+endpoint+']:'+str(port),'-f',str(Path.home()/'.ssh/known_hosts')],capture_output=True,text=True)
    if known.returncode or not known.stdout.strip():raise RuntimeError('trusted_host_key_missing')
    (private/'collector-known-hosts').write_text(known.stdout,encoding='utf-8')
    password_file=private/'diagnostics-password'
    if not password_file.exists():password_file.write_text(secrets.token_urlsafe(36),encoding='utf-8')
    stamp=datetime.now(timezone.utc).isoformat();assets=[];mapping={}
    def add(aid,kind,name,identity,extra=None):
        core_services={'frontend','api','gateway','emqx','tdengine','postgres','redis','ai-predict','feishu-adapter','agent','agent-rag','qdrant'}
        criticality='core' if kind in ('host','database','service') or identity.get('service') in core_services else 'support'
        assets.append(dict(asset_id=aid,kind=kind,display_name=name,project_scope='powersystem',parent_id=None if kind=='host' else 'r730xd',
                    enabled=True,criticality=criticality,environment='production',version=1,
                    verified_at=stamp,connection_ref='r730xd-ssh',identity_hash=digest(identity),capabilities=[t for t,(k,_) in TOOLS.items() if k==kind]))
        mapping[aid]={'identity':identity,**(extra or {})}
    add('r730xd','host','R730XD项目运行主机',{'machine_hash':probe['machine_hash'],'project':'powersystem-stage4'},
        {'mounts':[{'alias':'root-filesystem','path':'/'},{'alias':'project-filesystem','path':'/opt/powersystem'}]})
    for c in probe['containers']:
        service=c['service'];identity={k:c[k] for k in ('container_id','image_id','project','service')}
        add('r730xd-'+service,'container',service+' 项目容器',identity)
    for unit in probe['units']:
        add('r730xd-'+unit['unit'].removesuffix('.service'),'service',unit['unit'].removesuffix('.service')+' 项目服务',unit)
    pg=next(c for c in probe['containers'] if c['service']=='postgres')
    network=next((n for n in pg['networks'] if n.startswith('powersystem-stage4_')),None)
    if not network:raise ValueError('postgres_project_network_missing')
    add('r730xd-business-db','database','平台PostgreSQL数据库',{'database':'powersystem'},
        {'postgres_container_asset':'r730xd-postgres','network':network,'password_file':'/etc/powersystem-ops-read/db-password'})
    validate_catalog(assets);registry_hash=digest(assets)
    # Re-running an owned installation preserves immutable catalog versions and verification timestamps.
    prior=private/'remote-registry.json'
    if prior.exists():
        previous=json.loads(prior.read_text(encoding='utf-8'))
        old={a['asset_id']:a for a in previous['assets']}
        for a in assets:
            if a['asset_id'] in old:
                a['verified_at']=old[a['asset_id']]['verified_at']
                comparable=lambda value:{k:v for k,v in value.items() if k not in ('version','verified_at')}
                a['version']=old[a['asset_id']]['version']+(comparable(a)!=comparable(old[a['asset_id']]))
        registry_hash=digest(assets)
    registry={'assets':assets,'private_assets':mapping}
    prior.write_text(json.dumps(registry,ensure_ascii=False,indent=2),encoding='utf-8')
    public={'assets':assets};(private/'executor-registry.json').write_text(json.dumps(public,ensure_ascii=False),encoding='utf-8')
    wheels={p.name:base64.b64encode(p.read_bytes()).decode() for p in (private/'wheels').glob('*.whl')}
    if not wheels:raise ValueError('offline_wheels_required')
    code={name:base64.b64encode((root/'stages/09-ops-agent'/name).read_bytes()).decode() for name in ('ops_contract.py','collector/__init__.py','collector/broker.py')}
    payload={'code':code,'wheels':wheels,'registry':registry,'registry_hash':registry_hash,
             'grant_key':(private/'executor-grant-key').read_text().strip(),'db_password':password_file.read_text().strip(),
             'postgres_id':pg['container_id'],'sql':(root/'stages/09-ops-agent/deploy/003_diagnostics.sql').read_text(encoding='utf-8'),
             'ssh_entry':(root/'stages/09-ops-agent/collector/ssh-entry.py').read_text(encoding='utf-8'),
             'public_key':restricted_key.with_suffix('.pub').read_text().strip()}
    source='PAYLOAD='+repr(base64.b64encode(json.dumps(payload,ensure_ascii=False).encode()).decode())+'\n'+REMOTE_INSTALL
    compile(source,'<collector-install>','exec')
    result=remote(source,'install')
    (attempt/'installation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    config={'token_file':str(private/'executor-token'),'grant_key_file':str(private/'executor-grant-key'),
            'registry_file':str(private/'executor-registry.json'),'callback_url':'http://127.0.0.1:8096','ssh_executable':str(ssh),
            'hosts':{'r730xd-ssh':{'hostname':endpoint,'port':port,'private_key_file':str(restricted_key),'known_hosts_file':str(private/'collector-known-hosts')}}}
    (private/'executor.json').write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding='utf-8')
    print('Project R0 collector installed; '+str(len(assets))+' verified assets; no business restart or public port change.')


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--run',required=True);p.add_argument('--attempt',required=True);main(p.parse_args())
