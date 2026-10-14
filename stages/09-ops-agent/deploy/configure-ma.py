"""Provision only the specified M4 release; split model/control/transport secrets."""
import argparse,json,os,re,secrets,shutil,subprocess
from pathlib import Path
from urllib.parse import quote
ROOT=Path('/opt/powersystem')
def run(argv,**kw):
    p=subprocess.run(argv,capture_output=True,**kw)
    if p.returncode:raise RuntimeError('private_setup_failed:'+Path(argv[0]).name)
    return p.stdout
def readenv(p):return dict(l.split('=',1) for l in p.read_text().splitlines() if '=' in l and not l.lstrip().startswith('#'))
def envwrite(p,v):p.write_text(''.join(k+'='+str(value)+'\n' for k,value in v.items()));p.chmod(0o600)
def main(a):
    r=a.release.resolve()
    if os.uname().nodename!='R730XD' or not r.is_relative_to(ROOT/'runtime') or not re.fullmatch('ma-m4-[a-z0-9-]{1,65}',r.name) or not (r/'baseline.json').exists():raise ValueError('prepared_release_required')
    private=r/'private';source=private/'model-upload'
    if (private/'compose.env').exists():raise ValueError('new_configuration_required')
    for name in ('ops-llm-key','sec-llm-key'):
        if len((source/name).read_text().strip())<32:raise ValueError('private_model_keys_required')
    base=readenv(private/'compose-before.env')
    ragpath=Path(base['RAG_ENV_FILE']);ragpath=ragpath if ragpath.is_absolute() else ROOT/ragpath
    if not ragpath.resolve().is_relative_to(ROOT/'runtime'):raise ValueError('actual_rag_config_required')
    rag=readenv(ragpath)
    for name,uid in (('ops',10003),('security',10004),('executor',10005),('supervisor',10002),('api',int(run(['docker','exec','powersystem-stage4-api-1','id','-u']).decode()))):
        p=private/name;p.mkdir(mode=0o700);os.chown(p,uid,uid)
    def save(folder,name,value,uid):
        p=private/folder/name;p.write_text(value);p.chmod(0o600);os.chown(p,uid,uid)
    tokens={name:secrets.token_urlsafe(36) for name in ('ops-token','actor-key','envelope-key','executor-token','sec-service-token','sec-request-key','sec-result-key','db-password')}
    for name in ('ops-token','actor-key','envelope-key','executor-token','sec-service-token','sec-request-key','sec-result-key','db-password'):save('ops',name,tokens[name],10003)
    for name in ('sec-service-token','sec-request-key','sec-result-key'):save('security',name,tokens[name],10004)
    for name in ('ops-token','actor-key','envelope-key'):save('supervisor',name,tokens[name],10002)
    api_uid=int(run(['docker','exec','powersystem-stage4-api-1','id','-u']).decode())
    for name in ('ops-token','actor-key'):save('api',name,tokens[name],api_uid)
    save('executor','executor-token',tokens['executor-token'],10005)
    collector=Path('/etc/powersystem-ops-read')
    if json.loads((collector/'installation.json').read_text()).get('owner')!='powersystem-ma-m2':raise ValueError('owned_collector_required')
    # Preserve the existing collector identity and grant key, so old signed R0 readers remain compatible.
    grant=(collector/'grant-key').read_text().strip()
    save('ops','executor-grant-key',grant,10003);save('executor','executor-grant-key',grant,10005)
    save('ops','ops-llm-key',(source/'ops-llm-key').read_text().strip(),10003)
    save('security','sec-llm-key',(source/'sec-llm-key').read_text().strip(),10004)
    rag_token_source=Path(base['RAG_SERVICE_TOKEN_FILE']);save('ops','rag-service-token',rag_token_source.read_text().strip(),10003)
    transport=private/'executor/collector-ed25519'
    run(['ssh-keygen','-q','-t','ed25519','-N','','-f',str(transport)])
    transport.chmod(0o600);os.chown(transport,10005,10005)
    pub=transport.with_suffix('.pub').read_text().strip()
    auth=Path('/var/lib/powersystem-ops-read/.ssh/authorized_keys')
    before=auth.read_text();(private/'collector-authorized-keys-before').write_text(before);(private/'collector-authorized-keys-before').chmod(0o600)
    auth.write_text(before.rstrip()+'\nrestrict,command="/usr/bin/python3 -I /usr/local/libexec/powersystem-ops-ssh-entry" '+pub+'\n');auth.chmod(0o644)
    known='host.docker.internal '+Path('/etc/ssh/ssh_host_ed25519_key.pub').read_text().strip()+'\n'
    save('executor','known-hosts',known,10005)
    registry=json.loads((collector/'registry.json').read_text())
    save('executor','registry.json',json.dumps({'assets':registry['assets']},ensure_ascii=False),10005)
    executor={'token_file':'/run/ma-executor/executor-token','grant_key_file':'/run/ma-executor/executor-grant-key','registry_file':'/run/ma-executor/registry.json','callback_url':'http://ops-agent:8094','ssh_executable':'/usr/bin/ssh','security_required':True,'hosts':{}}
    for asset in registry['assets']:
        if asset['kind']=='host':executor['hosts'][asset['connection_ref']]={'hostname':'host.docker.internal','port':22,'private_key_file':'/run/ma-executor/collector-ed25519','known_hosts_file':'/run/ma-executor/known-hosts'}
    save('executor','executor.json',json.dumps(executor,ensure_ascii=False),10005)
    password=tokens['db-password'];rp=base['REDIS_PASSWORD']
    ops={'OPS_RUNTIME_PROFILE':'m4-server-readonly','OPS_ENABLED':'true','OPS_INFRA_ENABLED':'true','OPS_WRITE_ENABLED':'false','OPS_SECURITY_ENABLED':'true','OPS_INSPECTION_ENABLED':'true','MA_DRILL_ACCEPTANCE_ONLY':'false','OPS_DATABASE_URL':'postgresql://ma_ops_runtime:'+quote(password,safe='')+'@postgres:5432/ma_ops','OPS_REDIS_URL':'redis://:'+quote(rp,safe='')+'@redis:6379/10','OPS_REDIS_PREFIX':'ma:server-m4:','OPS_BACKEND_URL':'http://api:8080','OPS_SUPERVISOR_URL':'http://agent-rag:8093','OPS_EXECUTOR_URL':'http://ops-executor:8095','OPS_SECURITY_URL':'http://security-agent:8100','OPS_LLM_URL':'https://hgapi.dieqiyun.top/v1','OPS_MODEL':'deepseek-v4-flash-0731','OPS_AUDIT_DIR':'/audit','OPS_LLM_API_KEY_FILE':'/run/ma/ops-llm-key','OPS_SERVICE_TOKEN_FILE':'/run/ma/ops-token','MA_ACTOR_KEY_FILE':'/run/ma/actor-key','MA_ENVELOPE_KEY_FILE':'/run/ma/envelope-key','OPS_EXECUTOR_TOKEN_FILE':'/run/ma/executor-token','OPS_EXECUTOR_GRANT_KEY_FILE':'/run/ma/executor-grant-key','SEC_SERVICE_TOKEN_FILE':'/run/ma/sec-service-token','SEC_REQUEST_KEY_FILE':'/run/ma/sec-request-key','SEC_RESULT_KEY_FILE':'/run/ma/sec-result-key','AGENT_SERVICE_TOKEN_FILE':'/run/ma/rag-service-token'}
    envwrite(private/'ops.env',ops)
    sec={'SEC_RUNTIME_PROFILE':'m4-server-readonly','SEC_ENABLED':'true','SEC_MODEL':'deepseek-v4-flash-0731','SEC_LLM_URL':'https://hgapi.dieqiyun.top/v1','SEC_MODEL_TIMEOUT_SECONDS':'30','SEC_REVIEW_TTL_SECONDS':'300','SEC_ACCEPTANCE_TIMEOUT_EXCEPTION_ENABLED':'false','SEC_LLM_API_KEY_FILE':'/run/ma-security/sec-llm-key'}
    for name in ('service-token','request-key','result-key'):sec['SEC_'+name.replace('-','_').upper()+'_FILE']='/run/ma-security/sec-'+name
    envwrite(private/'security.env',sec)
    rag.update(OPS_URL='http://ops-agent:8094',OPS_SERVICE_TOKEN_FILE='/run/ma-supervisor/ops-token',MA_ACTOR_KEY_FILE='/run/ma-supervisor/actor-key',MA_ENVELOPE_KEY_FILE='/run/ma-supervisor/envelope-key',MA_DRILL_ACCEPTANCE_ONLY='false')
    envwrite(private/'rag.env',rag)
    audit=r/'ops-audit';audit.mkdir(mode=0o700);os.chown(audit,10003,10003)
    base.update(MA_OPS_IMAGE='powersystem-ma-ops-agent:'+r.name,MA_SECURITY_IMAGE='powersystem-ma-security-agent:'+r.name,MA_EXECUTOR_IMAGE='powersystem-ma-ops-executor:'+r.name,MA_OPS_ENV_FILE=private/'ops.env',MA_SECURITY_ENV_FILE=private/'security.env',MA_RAG_ENV_FILE=private/'rag.env',MA_PRIVATE_DIR=private/'ops',MA_SECURITY_PRIVATE_DIR=private/'security',MA_EXECUTOR_PRIVATE_DIR=private/'executor',MA_SUPERVISOR_PRIVATE_DIR=private/'supervisor',MA_API_PRIVATE_DIR=private/'api',MA_OPS_AUDIT_DIR=audit)
    envwrite(private/'compose.env',base)
    print(json.dumps({'configured':True,'server_write_enabled':False,'new_public_ports':0,'secret_groups':5}))
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--release',type=Path,required=True);main(p.parse_args())
