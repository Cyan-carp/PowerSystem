"""Remote-only candidate builder and controlled publisher. No raw config output."""
import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time

ROOT=Path('/opt/powersystem')
B=Path(__file__).resolve().parents[4]
C=B/'candidate/stages'
CONTROLLER=ROOT/'stages/04-frontend/deploy/production-stack.py'
RELEASE=B.name


def save(name,value):
    path=B/name;path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2));path.chmod(0o600)


def phase(value):
    save('progress.json',{'phase':value,'at':dt.datetime.now(dt.timezone.utc).isoformat()})
    print(value,flush=True)


def run(args,log,timeout=300,input=None):
    path=B/log
    with path.open('wb') as output:
        path.chmod(0o600)
        p=subprocess.Popen(args,cwd=ROOT,stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,stdout=output,stderr=subprocess.STDOUT)
        try:p.communicate(input=input,timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill();p.wait();raise RuntimeError('command_failed:'+log)
    if p.returncode:raise RuntimeError('command_failed:'+log)
    return path.read_bytes()


def controller():
    spec=importlib.util.spec_from_file_location('production_controller',CONTROLLER)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def source_copy(stage,overrides):
    target=B/('build-'+stage)
    if not target.exists():
        shutil.copytree(ROOT/'stages'/stage,target,ignore=shutil.ignore_patterns('node_modules','dist','.cache','__pycache__','.git','.env','*.env','*.key','*.pem','*secret*','*.log'))
    for name in overrides:
        source=C/stage/name;destination=target/name;destination.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(source,destination)
    return target


API_FILES=['cmd/api/main.go','internal/config/config.go','internal/model/model.go',
 'internal/api/server.go','internal/api/interpretation.go','internal/api/interpretation_v2.go','internal/api/interpretation_v2_test.go',
 'internal/api/simulation.go','internal/api/simulation_test.go','internal/api/alarm_maintenance.go','internal/api/interpretation_fixture_test.go',
 'internal/api/alarms.go','internal/api/alarm_statistics.go','internal/api/prediction.go','internal/api/business_notify.go',
 'internal/service/alarm.go','internal/service/notification.go','internal/service/simulation.go','internal/service/simulation_test.go',
 'deploy/postgres/009_simulation_alarms.sql','deploy/postgres/010_interpretation_v2.sql','deploy/postgres/011_alarm_maintenance.sql','scripts/reset-business-alarms.sql']
AGENT_FILES=['config.py','llm.py','main.py','interpret.py','interpret_provider.py','interpret_v2.py']
FRONT_FILES=['src/types.ts','src/lib/api.ts','src/lib/interpretations.ts','src/lib/interpretation-display.ts','src/lib/health.ts',
 'src/components/InterpretationPanel.vue','src/views/AlarmsView.vue']


def build():
    ctrl=controller();old,manifest=ctrl.active();ctrl.current_matches(old,manifest);ctrl.healthy(ctrl.inspect())
    if (B/'ready.json').exists():raise RuntimeError('ready_candidate_already_sealed')
    save('parent.json',{'release':old.name,'images':manifest['images']})
    images={}
    # Reuse the actual deployed Agent image, retaining its legacy orchestrator,
    # dependencies, and knowledge rather than publishing unrelated local code.
    ctx=B/'build-agent';(ctx/'agent').mkdir(parents=True,exist_ok=True)
    for name in AGENT_FILES:shutil.copy2(C/'07-agent/agent'/name,ctx/'agent'/name);(ctx/'agent'/name).chmod(0o644)
    base='powersystem-agent:'+RELEASE+'-base'
    run(['docker','tag',manifest['images']['agent'],base],'agent-base-tag.log')
    (ctx/'Dockerfile').write_text('FROM '+base+'\nUSER root\nCOPY agent/ /app/agent/\nRUN chmod 0644 /app/agent/*.py\nUSER 10001:10001\n')
    stages=[('agent',ctx),('api',source_copy('02-backend',API_FILES)),
            ('frontend',source_copy('04-frontend',FRONT_FILES)),
            ('feishu-adapter',source_copy('05-feishu-notification/feishu-adapter',['app.py','test_app.py']))]
    api_context=stages[1][1]
    dockerfile=api_context/'Dockerfile'
    text=dockerfile.read_text()
    # Reuse the already compiled candidate builder as a dependency/cache base.
    # Only build context files are copied; no production runtime secrets exist in it.
    cached='powersystem-interpret-tests:'+RELEASE
    if subprocess.run(['docker','image','inspect',cached],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0:
        cachebase=cached+'-cache'
        run(['docker','tag',cached,cachebase],'api-cache-tag.log',30)
        text=text.replace('FROM golang:1.24-alpine AS build','FROM '+cachebase+' AS build')
    if 'interpret-v2-tests' not in text:
        text=text.replace('FROM alpine:3.21','RUN CGO_ENABLED=0 go test -c -o /out/interpret-v2-tests ./internal/api\n\nFROM alpine:3.21')
        dockerfile.write_text(text)
    for service,context in stages:
        phase('building_'+service)
        tag='powersystem-'+service+':'+RELEASE
        run(['env','DOCKER_BUILDKIT=0','docker','build','--pull=false','-t',tag,str(context)],service+'-build.log',600)
        images[service]=run(['docker','image','inspect',tag,'--format','{{.Id}}'],service+'-image.log').decode().strip()
    run(['env','DOCKER_BUILDKIT=0','docker','build','--target','build','--pull=false','-t','powersystem-interpret-tests:'+RELEASE,str(api_context)],'api-test-builder.log',300)
    phase('candidate_import_and_unit_tests')
    run(['docker','run','--rm','--label','com.docker.compose.project=powersystem-interpret-candidate','--label','com.docker.compose.oneoff=True','--network','none','-v',str(C/'07-agent/tests')+':/qa/tests:ro','-e','PYTHONPATH=/qa:/app',
         '--entrypoint','python',images['agent'],'-m','unittest','tests.test_interpret_v2','-v'],'image-agent-tests.log',90)
    # App factory import/route registration is part of test_interpret_v2, avoiding
    # a redundant second container solely for the same import assertion.
    # No data deletion or mutation of the running production services occurs here.
    ctrl.current_matches(old,manifest)
    save('built.json',{'parent':old.name,'images':images,'agent_sources':{n:sha(ctx/'agent'/n) for n in AGENT_FILES},'api_sources':{n:sha(api_context/n) for n in API_FILES},'front_sources':{n:sha(stages[2][1]/n) for n in FRONT_FILES}})
    phase('built_awaiting_final_gates')


def repair_build():
    registry_only = RELEASE in {'interpretation-official-20261021-r3','interpretation-official-20261021-r4'}
    prior=ROOT/('runtime/interpretation-official-20261021-r3' if RELEASE.endswith('-r4') else 'runtime/interpretation-official-20261021-r2' if registry_only else 'runtime/interpretation-official-20261021-r1')
    baseline=json.loads((prior/'built.json').read_text())
    ctrl=controller();old,manifest=ctrl.active();ctrl.current_matches(old,manifest);ctrl.healthy(ctrl.inspect())
    assert old.name==baseline['parent'] and not (B/'ready.json').exists()
    for stage,files,exceptions in [('07-agent/agent',baseline['agent_sources'],set()),('02-backend',baseline['api_sources'],set() if registry_only else {'deploy/postgres/010_interpretation_v2.sql','internal/api/interpretation_fixture_test.go'}),('04-frontend',baseline['front_sources'],set())]:
        assert all(sha(C/stage/name)==expected for name,expected in files.items() if name not in exceptions),'repair_scope_changed'
    if registry_only:
        # The database is the committed asset-version authority. Patch only the
        # reviewed arithmetic in the live helper; old frozen releases stay intact.
        helper=ROOT/'stages/09-ops-agent/deploy/ma-release.py'
        before=helper.read_text()
        old_line='    return max(current,retained)+1'
        assert before.count(old_line) in {0,1}
        new_line='    return retained+1'
        reviewed=(C/'09-ops-agent/deploy/ma-release.py').read_text()
        assert new_line in reviewed and old_line not in reviewed
        shutil.copy2(helper,B/'registry-helper-before.py')
        if old_line in before:
            temporary=helper.with_suffix('.interpret-release-tmp')
            temporary.write_text(before.replace(old_line,new_line));temporary.chmod(helper.stat().st_mode & 0o777)
            os.replace(temporary,helper)
        else:assert before.count(new_line)==1
        for name in ['official-gate.json','image-agent-tests.log','fixture.json']:shutil.copy2(prior/name,B/name)
        save('reused-evidence.json',{'source':prior.name,'reason':'All four application images and tested sources unchanged. Only host release asset-version allocation corrected. Two local targeted tests passed.','new_model_calls':0,'host_helper_sha256':sha(helper)})
        save('built.json',baseline)
        phase('built_awaiting_final_gates')
        return
    phase('building_migration_only_api_layer')
    context=B/'migration-layer';context.mkdir(exist_ok=True)
    shutil.copy2(C/'02-backend/deploy/postgres/010_interpretation_v2.sql',context/'010_interpretation_v2.sql')
    (context/'Dockerfile').write_text('FROM '+baseline['images']['api']+'\nCOPY 010_interpretation_v2.sql /app/deploy/postgres/010_interpretation_v2.sql\n')
    tag='powersystem-api:'+RELEASE
    run(['env','DOCKER_BUILDKIT=0','docker','build','--pull=false','-t',tag,str(context)],'api-build.log',120)
    images=dict(baseline['images']);images['api']=run(['docker','image','inspect',tag,'--format','{{.Id}}'],'api-image.log').decode().strip()
    for name in ['official-gate.json','image-agent-tests.log']:shutil.copy2(prior/name,B/name)
    save('reused-evidence.json',{'source':prior.name,'reason':'Agent and frontend source/image unchanged; only SQL comment and migration test changed','new_model_calls':0})
    save('built.json',{'parent':old.name,'images':images,'agent_sources':baseline['agent_sources'],'front_sources':baseline['front_sources'],
      'api_sources':{name:sha(C/'02-backend'/name) for name in API_FILES},'test_builder':'powersystem-interpret-tests:'+prior.name})
    phase('built_awaiting_final_gates')


def progress():
    out={}
    for name in ['progress.json','built.json','fixture.json','ready.json','published.json','reset.json','postcheck.json']:
        if (B/name).exists():
            value=json.loads((B/name).read_text())
            if name=='built.json':value={key:value[key] for key in ['parent','images']}
            if name=='ready.json':value={'parent':value['parent'],'at':value['at'],'official_gate_passed':value['official_gate']['passed'],'official_calls':value['official_gate']['calls'],'fixture_passed':value['fixture']['passed']}
            out[name]=value
    failures=sorted(B.glob('failure-*.json'))
    if failures:out['last_failure']=json.loads(failures[-1].read_text())
    for name in ['backup.log','backup-readability.log','publish.log','reset-transaction.log']:
        if (B/name).exists():out[name+'_bytes']=(B/name).stat().st_size
    for name in ['fixture-test.log','image-agent-tests.log']:
        if (B/name).exists():out[name]=(B/name).read_text(errors='replace').splitlines()[-18:]
    log=B/'build-driver.log'
    if log.exists():out['driver_lines']=log.read_text(errors='replace').splitlines()[-8:]
    out['build_log_sizes']={p.name:p.stat().st_size for p in B.glob('*build*.log')}
    out['build_log_tails']={p.name:p.read_text(errors='replace').splitlines()[-6:] for p in B.glob('*-build.log')}
    processes=subprocess.check_output(['ps','-eo','etime,pcpu,comm'],text=True)
    out['build_processes']=[line.strip() for line in processes.splitlines() if line.split()[-1] in {'go','compile','link','docker','npm'}][-12:]
    dockerfile=B/'build-02-backend/Dockerfile'
    if dockerfile.exists():out['api_builder_from']=[line for line in dockerfile.read_text().splitlines() if line.startswith('FROM ')]
    out['cached_build_images']=subprocess.check_output(['docker','images','--filter','reference=*golang*','--format','{{.Repository}}:{{.Tag}}'],text=True).splitlines()
    out['go_build_containers']=subprocess.check_output(['docker','ps','--filter','ancestor=golang:1.24-alpine','--format','{{.Names}}|{{.Command}}'],text=True).splitlines()
    context=B/'build-02-backend'
    if context.exists():out['api_context_bytes']=sum(p.stat().st_size for p in context.rglob('*') if p.is_file())
    ids=subprocess.check_output(['docker','ps','-aq','--filter','label=com.docker.compose.project=powersystem-interpret-candidate'],text=True).split()
    if ids:
        rows=json.loads(subprocess.check_output(['docker','inspect',*ids],text=True))
        out['candidate_containers']=[{'id':r['Id'][:12],'name':r['Name'],'status':r['State']['Status'],'exit':r['State']['ExitCode'],'command':r['Config'].get('Cmd'),'volumes':[m['Type'] for m in r['Mounts']]} for r in rows]
        out['candidate_log_tails']={r['Id'][:12]:subprocess.run(['docker','logs','--tail','12',r['Id']],capture_output=True,text=True).stdout for r in rows}
    print(json.dumps(out))


def cleanup_fixture():
    for name in ['powersystem-interpret-fixture-redis','powersystem-interpret-fixture-pg']:
        check=subprocess.run(['docker','inspect',name],capture_output=True,text=True,timeout=15)
        if check.returncode:continue
        row=json.loads(check.stdout)[0]
        assert row['Config']['Labels'].get('com.docker.compose.project')=='powersystem-interpret-candidate'
        assert row['Name']=='/'+name and not any(m['Type']=='volume' for m in row['Mounts']) and not row['HostConfig'].get('PortBindings')
        subprocess.run(['docker','rm','-f',row['Id']],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=45,check=True)


def fixture():
    phase('isolated_postgres_redis_fixture')
    cleanup_fixture()
    pg='powersystem-interpret-fixture-pg';redis='powersystem-interpret-fixture-redis'
    images={name:subprocess.check_output(['docker','inspect','--format','{{.Image}}','powersystem-stage4-'+name+'-1'],text=True).strip() for name in ['postgres','redis']}
    labels=['--label','com.docker.compose.project=powersystem-interpret-candidate','--label','com.docker.compose.oneoff=True']
    created=[]
    try:
        run(['docker','run','-d','--name',pg,*labels,'--network','none','--memory','512m','--cpus','1','--tmpfs','/var/lib/postgresql/data:rw,size=256m',
             '-e','PGDATA=/var/lib/postgresql/data','-e','POSTGRES_USER=powersystem','-e','POSTGRES_PASSWORD=fixture-only','-e','POSTGRES_DB=interpret_fixture',images['postgres']], 'fixture-pg-start.log',90);created.append(pg)
        for _ in range(100):
            if subprocess.run(['docker','exec',pg,'pg_isready','-U','powersystem','-d','interpret_fixture'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0:break
            time.sleep(.2)
        else:raise RuntimeError('fixture_not_ready')
        for sql in sorted((C/'02-backend/deploy/postgres').glob('[0-9][0-9][0-9]_*.sql')):
            run(['docker','exec','-i',pg,'psql','-U','powersystem','-d','interpret_fixture','-v','ON_ERROR_STOP=1'], 'fixture-'+sql.stem+'.log',20,input=sql.read_bytes())
        run(['docker','run','-d','--name',redis,*labels,'--network','container:'+pg,'--memory','128m','--cpus','1','--tmpfs','/data',images['redis'],'redis-server','--save','','--appendonly','no'],'fixture-redis-start.log',90);created.append(redis)
        built=json.loads((B/'built.json').read_text())
        test_builder=built.get('test_builder','powersystem-interpret-tests:'+RELEASE)
        test_args=['--entrypoint','/out/interpret-v2-tests',test_builder,'-test.run','^TestV2PostgresRedisFixture$','-test.v']
        mounts=[]
        if 'test_builder' in built:
            mounts=['-v',str(C/'02-backend/internal/api/interpretation_fixture_test.go')+':/src/internal/api/interpretation_fixture_test.go:ro',
                    '-v',str(C/'02-backend/deploy/postgres/010_interpretation_v2.sql')+':/src/deploy/postgres/010_interpretation_v2.sql:ro',
                    '-e','CGO_ENABLED=0','-e','GOPROXY=off']
            test_args=['--entrypoint','go',test_builder,'test','./internal/api','-run','^TestV2PostgresRedisFixture$','-count=1','-v']
        test_directory='/src' if 'test_builder' in built else '/src/internal/api'
        run(['docker','run','--rm',*labels,'--network','container:'+pg,'--workdir',test_directory,*mounts,
             '-v',str(ROOT/'stages/01-data-chain/simulator/fleet.json')+':/01-data-chain/simulator/fleet.json:ro',
             '-e','INTERPRET_FIXTURE_DSN=host=127.0.0.1 user=powersystem password=fixture-only dbname=interpret_fixture port=5432 sslmode=disable',
             *test_args], 'fixture-test.log',150)
        assert b'PASS' in (B/'fixture-test.log').read_bytes() and b'SKIP' not in (B/'fixture-test.log').read_bytes()
        save('fixture.json',{'passed':True,'covers':['PG_migrations','five_slots_concurrency','auto_end','restart_and_day_boundary','real_device_bypass','prediction_preservation','persistent_two_call_budget','lease_fencing','Redis_circuit_half_open_recovery','reset_dependency_and_sequence_preservation'],'production_mutated':False})
        phase('fixture_passed')
    finally:
        cleanup_fixture()


def sql(statement):
    p=subprocess.run(['docker','exec','-i','powersystem-stage4-postgres-1','psql','-U','powersystem','-d','powersystem','-At','-v','ON_ERROR_STOP=1'],input=statement.encode(),capture_output=True,timeout=25)
    if p.returncode:raise RuntimeError('database_operation_failed')
    return p.stdout.decode().strip()


def private_environment_path(original, executor, release):
    # The legacy registry helper resolves private/executor relative to the
    # compose environment. Preserve that established layout and real mount.
    original=Path(original)
    directory=(original.parent/'executor').resolve()
    assert original.is_file() and (directory/'registry.json').is_file(), 'missing_executor_registry_layout'
    assert any(m['Type']=='bind' and Path(m['Source']).resolve()==directory and m['Destination']=='/run/ma-executor'
               for m in executor['Mounts']), 'executor_registry_mount_mismatch'
    target=original.with_name(release+'.env')
    assert not target.exists(), 'private_candidate_environment_exists'
    return target


def prepare():
    built=json.loads((B/'built.json').read_text());gate=json.loads((B/'official-gate.json').read_text());test=json.loads((B/'fixture.json').read_text())
    assert gate['passed'] and gate['full_gate'] and gate['calls']==6 and test['passed'],'required_gates_missing'
    assert all(sha(C/'07-agent/agent'/name)==expected for name,expected in built['agent_sources'].items()),'agent_sources_changed_after_gate'
    assert all(sha(C/'02-backend'/name)==expected for name,expected in built['api_sources'].items()),'api_sources_changed_after_build'
    assert all(sha(C/'04-frontend'/name)==expected for name,expected in built['front_sources'].items()),'frontend_sources_changed_after_build'
    ctrl=controller();old,manifest=ctrl.active();assert old.name==built['parent'];ctrl.current_matches(old,manifest)
    private=B/'private';private.mkdir(mode=0o700,exist_ok=True)
    args=json.loads((old/'private/origin-args.json').read_text())
    index=args.index('--env-file')+1
    original=Path(args[index]);newenv=private_environment_path(original,ctrl.inspect()['ops-executor'],RELEASE)
    newenv.write_text(original.read_text()+'\nAGENT_INTERPRET_LLM_API_KEY_FILE=/etc/powersystem/interpret-deepseek-key\n');newenv.chmod(0o600)
    args[index]=str(newenv)
    frozen=json.loads((old/'private/compose.json').read_text())
    secrets=frozen['services']['agent'].get('secrets',[])
    secrets=[s for s in secrets if (s if isinstance(s,str) else s['source'])!='agent_interpret_llm_key']
    secrets.append({'source':'agent_interpret_llm_key','target':'agent_interpret_llm_key'})
    sim=list(frozen['services']['simulator']['command']);assert '--fault-mode' in sim;sim[sim.index('--fault-mode')+1]='off'
    overlay={'services':{'api':{'environment':{'AGENT_INTERPRET_PROTOCOL':'v2','SIMULATION_ALARM_MODE':'controlled'}},
        'agent':{'environment':{'AGENT_INTERPRET_LLM_BASE_URL':'https://api.deepseek.com','AGENT_INTERPRET_LLM_MODEL':'deepseek-flash','AGENT_INTERPRET_THINKING':'disabled','AGENT_INTERPRET_LLM_API_KEY_FILE':'/run/secrets/agent_interpret_llm_key'},'secrets':secrets},
        'simulator':{'command':sim}},'secrets':{'agent_interpret_llm_key':{'file':'/etc/powersystem/interpret-deepseek-key'}}}
    save('private/official-overlay.json',overlay);args.extend(['-f',str(private/'official-overlay.json')]);save('private/origin-args.json',args)
    # Add only the new secret to the existing encrypted backup routine.
    backup=ROOT/'stages/04-frontend/deploy/backup.sh';text=backup.read_text()
    if 'interpret-deepseek-key' not in text:
        marker='  mkdir -m 700 "$staging/config/agent"\n'
        assert text.count(marker)==1,'backup_patch_baseline_changed'
        shutil.copy2(backup,B/'backup-script-before.sh')
        backup.write_text(text.replace(marker,marker+'  if [ -s /etc/powersystem/interpret-deepseek-key ]; then\n    cp -p /etc/powersystem/interpret-deepseek-key "$staging/config/agent/"\n  fi\n',1))
    command=['python3',str(CONTROLLER),'prepare','--id',RELEASE,'--from-args',str(private/'origin-args.json'),'--service','simulator']
    for name,image in built['images'].items():command.extend(['--image',name+'='+image])
    phase('preparing_frozen_candidate');run(command,'prepare.log',180)
    save('ready.json',{'parent':old.name,'images':built['images'],'official_gate':gate,'fixture':test,'at':dt.datetime.now(dt.timezone.utc).isoformat()})
    phase('prepared_awaiting_backup')


def publish():
    ready=json.loads((B/'ready.json').read_text());ctrl=controller();old,manifest=ctrl.active();assert old.name==ready['parent'];ctrl.current_matches(old,manifest)
    built=json.loads((B/'built.json').read_text())
    for stage,files in [('07-agent/agent',built['agent_sources']),('02-backend',built['api_sources']),('04-frontend',built['front_sources'])]:
        assert all(sha(C/stage/name)==expected for name,expected in files.items()),'sealed_sources_changed'
    phase('new_encrypted_predeployment_backup')
    run(['bash','stages/04-frontend/deploy/backup.sh'],'backup.log',900)
    archives=sorted(Path('/var/backups/powersystem').glob('powersystem-*.tar.gz.gpg'),key=lambda p:p.stat().st_mtime)
    archive=archives[-1];assert sha(archive)==archive.with_suffix(archive.suffix+'.sha256').read_text().split()[0]
    with (B/'backup-readability.log').open('wb') as log:
        decrypted=subprocess.Popen(['gpg','--batch','--pinentry-mode','loopback','--passphrase-file','/etc/powersystem/backup-passphrase','--decrypt',str(archive)],stdout=subprocess.PIPE,stderr=log)
        listing=subprocess.run(['tar','-tzf','-'],stdin=decrypted.stdout,stdout=subprocess.DEVNULL,stderr=log,timeout=180)
        decrypted.stdout.close();assert decrypted.wait(timeout=20)==0 and listing.returncode==0
    phase('publishing_controlled_stack')
    run(['python3',str(CONTROLLER),'publish','--id',RELEASE,'--backup',str(archive)],'publish.log',700)
    active,_=ctrl.active();assert active.name==RELEASE
    save('published.json',{'release':RELEASE,'parent':ready['parent'],'backup':archive.name,'backup_sha256':sha(archive),'backup_stream_readable':True,'at':dt.datetime.now(dt.timezone.utc).isoformat()})
    # Keep on-host source aligned with deployed images, retaining unrelated files.
    def atomic_copy(source,destination):
        destination.parent.mkdir(parents=True,exist_ok=True)
        temporary=destination.with_name(destination.name+'.interpret-release-tmp')
        shutil.copy2(source,temporary);assert sha(temporary)==sha(source)
        os.replace(temporary,destination)
    for name in API_FILES:atomic_copy(C/'02-backend'/name,ROOT/'stages/02-backend'/name)
    for name in AGENT_FILES:atomic_copy(C/'07-agent/agent'/name,ROOT/'stages/07-agent/agent'/name)
    for name in FRONT_FILES:atomic_copy(C/'04-frontend'/name,ROOT/'stages/04-frontend'/name)
    atomic_copy(C/'05-feishu-notification/feishu-adapter/app.py',ROOT/'stages/05-feishu-notification/feishu-adapter/app.py')
    phase('published_awaiting_protected_reset')


def alarm_snapshot():
    return json.loads(sql("""SELECT json_build_object(
      'alarms',(SELECT count(*) FROM alarm_records),'cutoff',(SELECT COALESCE(max(id),0) FROM alarm_records),
      'alarm_tasks',(SELECT count(*) FROM agent_interpretations WHERE alarm_id IS NOT NULL),
      'business_outbox',(SELECT count(*) FROM business_notification_outbox),
      'alarm_reads',(SELECT count(*) FROM agent_interpretation_reads r JOIN agent_interpretations i ON i.id=r.interpretation_id WHERE i.alarm_id IS NOT NULL),
      'monitor_events',(SELECT count(*) FROM agent_monitor_events),'monitor_tasks',(SELECT count(*) FROM agent_interpretations WHERE alarm_id IS NULL),
      'devices_hash',(SELECT md5(COALESCE(json_agg(row_to_json(d) ORDER BY d.id)::text,'')) FROM devices d),
      'rules_hash',(SELECT md5(COALESCE(json_agg(row_to_json(r) ORDER BY r.id)::text,'')) FROM alarm_rules r),
      'prediction_max_id',(SELECT COALESCE(max(id),0) FROM prediction_records),
      'telemetry_max_id',(SELECT COALESCE(max(id),0) FROM telemetry_inbox));"""))


def reset():
    import fcntl
    reset_lock=(ROOT/'runtime/production-stack/deployment.lock').open('a')
    fcntl.flock(reset_lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    assert (B/'published.json').exists() and not (B/'reset.json').exists()
    ctrl=controller();active,_=ctrl.active();assert active.name==RELEASE
    rows=ctrl.inspect();ctrl.healthy(rows);runtime_provider_ready(rows)
    assert sql('SELECT count(*) FROM simulation_alarm_policy_devices WHERE active')=='143'
    phase('freezing_alarm_writes_and_dispatch')
    sql('UPDATE alarm_maintenance_state SET enabled=TRUE,updated_at=now() WHERE id=1;')
    deleted=False
    try:
        time.sleep(10)
        for _ in range(50):
            if sql("SELECT count(*) FROM agent_interpretations WHERE task_status='running' AND lease_until>now()")=='0':break
            time.sleep(1)
        else:raise RuntimeError('inflight_interpretations_not_drained')
        before=alarm_snapshot();save('reset-before.json',before)
        phase('encrypted_frozen_alarm_snapshot')
        cipher=B/'business-alarms-before-reset.dump.gpg'
        with (B/'reset-backup.log').open('wb') as log:
            dump=subprocess.Popen(['docker','exec','powersystem-stage4-postgres-1','pg_dump','-U','powersystem','-d','powersystem','-Fc','-t','alarm_records','-t','agent_interpretations','-t','agent_interpretation_reads','-t','business_notification_outbox','-t','agent_scan_cursor'],stdout=subprocess.PIPE,stderr=log)
            encrypt=subprocess.Popen(['gpg','--batch','--yes','--pinentry-mode','loopback','--passphrase-file','/etc/powersystem/backup-passphrase','--symmetric','--cipher-algo','AES256','--output',str(cipher)],stdin=dump.stdout,stdout=subprocess.DEVNULL,stderr=log)
            dump.stdout.close();assert encrypt.wait(timeout=120)==0 and dump.wait(timeout=20)==0
        cipher.chmod(0o600)
        with (B/'reset-backup-readability.log').open('wb') as log:
            decrypt=subprocess.Popen(['gpg','--batch','--pinentry-mode','loopback','--passphrase-file','/etc/powersystem/backup-passphrase','--decrypt',str(cipher)],stdout=subprocess.PIPE,stderr=log)
            listing=subprocess.run(['docker','exec','-i','powersystem-stage4-postgres-1','pg_restore','--list'],stdin=decrypt.stdout,stdout=log,stderr=log,timeout=30)
            # pg_restore --list may stop after the TOC. Keep consuming the
            # remaining stream so GPG authenticates the entire ciphertext,
            # rather than rejecting a valid dump with an artificial SIGPIPE.
            for _ in iter(lambda:decrypt.stdout.read(1024*1024),b''):pass
            decrypt.stdout.close();assert decrypt.wait(timeout=20)==0 and listing.returncode==0
        assert alarm_snapshot()['cutoff']==before['cutoff'],'alarm_writes_not_drained'
        phase('deleting_old_business_alarms_transactionally')
        run(['docker','exec','-i','powersystem-stage4-postgres-1','psql','-U','powersystem','-d','powersystem','-v','ON_ERROR_STOP=1','-v','cutoff='+str(before['cutoff'])], 'reset-transaction.log',30,input=(C/'02-backend/scripts/reset-business-alarms.sql').read_bytes());deleted=True
        after=alarm_snapshot()
        assert after['alarms']==0 and after['alarm_tasks']==0 and after['business_outbox']==0 and after['alarm_reads']==0
        assert after['devices_hash']==before['devices_hash'] and after['rules_hash']==before['rules_hash']
        assert after['monitor_events']>=before['monitor_events'] and after['monitor_tasks']>=before['monitor_tasks']
        assert after['prediction_max_id']>=before['prediction_max_id'] and after['telemetry_max_id']>=before['telemetry_max_id']
        save('reset.json',{'passed':True,'before':before,'after':after,'encrypted_snapshot':cipher.name,'snapshot_sha256':sha(cipher),'identity_sequences_reset':False,'at':dt.datetime.now(dt.timezone.utc).isoformat()})
        sql('UPDATE alarm_maintenance_state SET enabled=FALSE,updated_at=now() WHERE id=1;')
        ctrl.healthy(ctrl.inspect());phase('finished')
    except Exception:
        if not deleted:sql('UPDATE alarm_maintenance_state SET enabled=FALSE,updated_at=now() WHERE id=1;')
        else:save('reset-requires-operator.json',{'maintenance_retained':True,'reason':'post_delete_verification_failed','recovery':'inspect reset-before.json and encrypted snapshot, then explicitly resume maintenance; do not restore old API'})
        raise


def runtime_provider_ready(rows):
    # Inspect only explicitly allowlisted non-secret configuration fields.
    agent=rows['agent'];env=dict(v.split('=',1) for v in agent['Config']['Env'])
    assert env.get('AGENT_INTERPRET_LLM_MODEL')=='deepseek-flash' and env.get('AGENT_INTERPRET_THINKING')=='disabled'
    health=json.loads(subprocess.check_output(['docker','exec',agent['Id'],'python','-c',"import json,urllib.request; print(json.dumps(json.load(urllib.request.urlopen('http://127.0.0.1:8092/health',timeout=5))))"],text=True,timeout=15))
    assert health.get('interpret_model_configured') is True
    api_env=dict(v.split('=',1) for v in rows['api']['Config']['Env'])
    assert api_env.get('AGENT_INTERPRET_PROTOCOL')=='v2' and api_env.get('SIMULATION_ALARM_MODE')=='controlled'


def postcheck():
    ctrl=controller();active,manifest=ctrl.active();ctrl.current_matches(active,manifest)
    rows=ctrl.inspect();ctrl.healthy(rows);runtime_provider_ready(rows)
    assert active.name==RELEASE and (B/'reset.json').exists()
    summary=json.loads(sql("""SELECT json_build_object('maintenance',(SELECT enabled FROM alarm_maintenance_state WHERE id=1),
      'controlled_devices',(SELECT count(*) FROM simulation_alarm_policy_devices WHERE active),
      'alarms',(SELECT count(*) FROM alarm_records),
      'today_slots',(SELECT count(*) FROM simulation_alarm_slots WHERE slot_date=(now() AT TIME ZONE 'Asia/Shanghai')::date),
      'today_fired',(SELECT count(*) FROM simulation_alarm_slots WHERE slot_date=(now() AT TIME ZONE 'Asia/Shanghai')::date AND started_at IS NOT NULL));"""))
    assert not summary['maintenance'] and summary['controlled_devices']==143 and summary['today_slots']==5
    built=json.loads((B/'built.json').read_text())
    for name,expected in built['agent_sources'].items():
        actual=subprocess.check_output(['docker','exec',rows['agent']['Id'],'sha256sum','/app/agent/'+name],text=True).split()[0]
        assert actual==expected
    value={'release':active.name,'pending':(ROOT/'runtime/production-stack/pending.json').exists(),'services_healthy':len(rows),
      'model':'deepseek-flash','thinking':'disabled','protocol':2,'summary':summary,'additional_model_calls':0,'at':dt.datetime.now(dt.timezone.utc).isoformat()}
    assert not value['pending'];save('postcheck.json',value);print(json.dumps(value))


def main():
    assert socket.gethostname()=='R730XD' and os.geteuid()==0
    os.umask(0o077)
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['build','repair-build','progress','fixture','prepare','publish','reset','postcheck'])
    action=parser.parse_args().action
    try:
        if action=='build':build()
        elif action=='repair-build':repair_build()
        elif action=='fixture':fixture()
        elif action=='prepare':prepare()
        elif action=='publish':publish()
        elif action=='reset':reset()
        elif action=='postcheck':postcheck()
        else:progress()
    except Exception as error:
        safe=str(error) if type(error).__name__=='Blocked' or str(error).startswith(('command_failed:','ready_candidate_')) else 'candidate_operation_failed'
        save('failure-'+str(time.time_ns())+'.json',{'error_type':type(error).__name__,'safe_reason':safe,'filename':getattr(error,'filename',None)})
        phase('stopped_needs_attention')
        raise SystemExit(1)


if __name__=='__main__':main()
