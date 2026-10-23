"""Bounded remote candidate operations; never reads or emits secret contents."""
import argparse
import io
import json
import re
from pathlib import Path
import shlex
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[3]
HOST = "root@192.168.111.2"
BATCH = "/opt/powersystem/runtime/interpretation-official-20261021-r4"


def remote(code, data=None, timeout=120):
    process = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
        "-o", "ConnectTimeout=10", HOST, "python3 -c " + shlex.quote(code)], input=data,
        capture_output=True, timeout=timeout)
    if process.returncode:
        # Raw SSH/command diagnostics can include vendor/compose secret values.
        print(json.dumps({"remote_failed": True, "exit_code": process.returncode,
            "exception_types": re.findall(r"^([A-Za-z_.]+(?:Error|Exception|Expired)):",process.stderr.decode('utf-8',errors='replace'),re.M)}))
        raise SystemExit(process.returncode)
    print(process.stdout.decode("utf-8", errors="replace"))


def package():
    paths = {ROOT/'stages/09-ops-agent/deploy/ma-release.py'}
    for stage in ("07-agent", "02-backend", "04-frontend", "05-feishu-notification"):
        folder = ROOT / "stages" / stage
        for path in folder.rglob("*"):
            rel = path.relative_to(folder)
            if any(p in {"node_modules", "dist", ".cache", ".git", "__pycache__", ".pytest_cache"} for p in rel.parts):
                continue
            if path.is_file() and (path.suffix in {".py", ".go", ".sql", ".ts", ".vue", ".css", ".json", ".sh"}
                    or path.name in {"Dockerfile", "requirements.txt", "go.mod", "go.sum", "package-lock.json"}):
                if any(part.startswith(".") for part in rel.parts) or "secret" in path.name.lower():
                    continue
                paths.add(path)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path in sorted(paths):
            archive.add(path, arcname=path.relative_to(ROOT).as_posix(), recursive=False)
    return buffer.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["status", "upload", "test-agent", "official-gate", "build", "repair-build", "progress", "fixture", "prepare", "publish", "reset", "postcheck", "cleanup-owned-probe", "cleanup-created-check"])
    parser.add_argument("--case",type=int,choices=range(1,7))
    args = parser.parse_args()
    preamble = "import json,os,socket,subprocess; from pathlib import Path\nassert socket.gethostname()=='R730XD' and os.geteuid()==0\n"
    if args.action == "status":
        remote(preamble + """
state=Path('/opt/powersystem/runtime/production-stack')
key=Path('/etc/powersystem/interpret-deepseek-key')
ids=subprocess.check_output(['docker','ps','-aq','--filter','label=com.docker.compose.project=powersystem-stage4'],text=True).split()
rows=json.loads(subprocess.check_output(['docker','inspect',*ids],text=True))
print(json.dumps({'pending':(state/'pending.json').exists(),'key_installed':key.is_file(),
 'key_mode':oct(key.stat().st_mode & 0o777) if key.exists() else None,
 'project_containers':[{'name':r['Name'],'id':r['Id'][:12],'running':r['State']['Running'],'service':r['Config']['Labels'].get('com.docker.compose.service'),'oneoff':r['Config']['Labels'].get('com.docker.compose.oneoff')} for r in rows]}))
""")
    elif args.action == "cleanup-created-check":
        remote(preamble+f"""
identifier='c528b11ee6a0';row=json.loads(subprocess.check_output(['docker','inspect',identifier],text=True))[0]
assert row['State']['Status']=='created' and row['Name']=='/hungry_jemison' and row['Config']['Labels'].get('com.docker.compose.project')=='powersystem-interpret-candidate'
assert row['Config']['Cmd']==['-c','from agent.main import app; assert any(r.path=="/internal/agent/interpret/v2" for r in app.routes); print("v2_app_import_ok")'] and not row['Mounts']
subprocess.check_call(['docker','rm',identifier],stdout=subprocess.DEVNULL)
print(json.dumps({{'removed_owned_unstarted_check':identifier,'data_removed':False}}))
""")
    elif args.action == "cleanup-owned-probe":
        remote(preamble+f"""
batch=Path({BATCH!r});identifier='4d5ad52cda08'
row=json.loads(subprocess.check_output(['docker','inspect',identifier],text=True))[0]
assert not row['State']['Running'] and row['Name']=='/amazing_beaver'
assert any(m['Type']=='bind' and m['Source']==str(batch/'candidate/stages/07-agent/agent') and m['Destination']=='/app/agent' and not m['RW'] for m in row['Mounts'])
assert not any(m['Type']=='volume' for m in row['Mounts'])
(batch/'owned-stopped-probe.log').write_bytes(subprocess.check_output(['docker','logs',identifier],stderr=subprocess.STDOUT))
subprocess.check_call(['docker','rm',identifier],stdout=subprocess.DEVNULL)
print(json.dumps({{'removed_owned_stopped_probe':identifier,'volumes_removed':False}}))
""")
    elif args.action == "upload":
        remote(preamble + f"""
import sys,tarfile,io
batch=Path({BATCH!r}); target=batch/'candidate'
target.mkdir(parents=True,exist_ok=True)
assert target.resolve().is_relative_to(Path('/opt/powersystem/runtime'))
with tarfile.open(fileobj=io.BytesIO(sys.stdin.buffer.read()),mode='r:gz') as archive:
 for member in archive.getmembers():
  assert member.isfile() and not member.name.startswith('/') and '..' not in Path(member.name).parts
 archive.extractall(target)
for item in [target,*target.rglob('*')]:
 if item.is_dir(): item.chmod(0o755)
 elif item.is_file(): item.chmod(0o644)
print(json.dumps({{'candidate_uploaded':True,'files':sum(1 for p in target.rglob('*') if p.is_file())}}))
""", package(), 90)
    elif args.action in {"build","repair-build","progress","fixture","prepare","publish","reset","postcheck"}:
        remote(preamble+f"""
batch=Path({BATCH!r});worker=batch/'candidate/stages/07-agent/scripts/interpretation-release-worker.py'
if {args.action!r} in {{'build','repair-build','fixture','prepare','publish','reset'}}:
 log=(batch/'build-driver.log').open('ab');logpath=batch/'build-driver.log';logpath.chmod(0o600)
 p=subprocess.Popen(['python3','-u',str(worker),{args.action!r}],stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
 print(json.dumps({{'operation_started':{args.action!r},'pid':p.pid}}))
else:
 p=subprocess.run(['python3',str(worker),{args.action!r}],capture_output=True,text=True,timeout=25);print(p.stdout)
""",timeout=30)
    elif args.action == "test-agent":
        remote(preamble + f"""
batch=Path({BATCH!r}); source=batch/'candidate/stages/07-agent'
image=subprocess.check_output(['docker','inspect','--format','{{{{.Image}}}}','powersystem-stage4-agent-1'],text=True).strip()
cmd=['docker','run','--rm','--label','com.docker.compose.project=powersystem-interpret-candidate','--label','com.docker.compose.oneoff=True','--network','none','-v',str(source/'agent')+':/app/agent:ro','-v',str(source/'tests')+':/qa/tests:ro','-e','PYTHONPATH=/qa:/app','--entrypoint','python',image,'-m','unittest','tests.test_interpret_v2','-v']
p=subprocess.run(cmd,capture_output=True,text=True,timeout=60)
(batch/'agent-unit.log').write_text(p.stdout+p.stderr)
print(json.dumps({{'agent_tests_passed':p.returncode==0,'output':(p.stdout+p.stderr)[-7000:]}}))
""", timeout=90)
    elif args.action == "official-gate":
        # Only the remote container opens the private key. No credentials enter
        # arguments, stdin, candidate files, or returned diagnostics.
        probe = r'''
import asyncio,json,time
from pathlib import Path
from unittest.mock import AsyncMock
import httpx
from agent.config import Config
from agent.interpret_provider import InterpretationProvider
from agent.interpret_v2 import probe_event,Event,Fact,run_event
from agent.schemas import Evidence
async def main():
 cfg=Config(backend_url='http://api',llm_url='',model='',api_key='',service_token='synthetic-probe-only',redis_url='redis://unused',audit_dir=Path('/tmp/audit'),interpret_api_key=Path('/run/secrets/event_key').read_text().strip(),interpret_model_configured=True)
 store=AsyncMock();store.get.return_value=None
 out=[]
 async with httpx.AsyncClient(follow_redirects=False,trust_env=False) as client:
  provider=InterpretationProvider(cfg,client,store)
  for index,category in enumerate(['demo','demo','prediction_risk','prediction_risk','device_alarm','platform_monitor']):
   if SELECTED_CASE and index+1!=SELECTED_CASE:continue
   event=probe_event();event.event_key='protocol:official-gate-'+str(index)
   if category=='demo':
    event.category='device_alarm';event.event_origin='simulation_demo'
    event.evidence[0].data.update(metric='simulation_scenario',value=1,threshold=0,scenario_label='受控演示，不代表设备实际故障')
    event.facts[0]=Fact(fact_id='F1',evidence_id='E1',path='scenario_label',label='事件性质',role='scenario_label',value_type='text',value='受控模拟演示',display_value='受控模拟演示')
   elif category=='platform_monitor':
    evidence=Evidence(id='E1',tool='monitor_event',status='ok',source='synthetic://model-check/monitor',collected_at='2026-10-21T08:00:00Z',data={'labels':{'alertname':'PowerSystemDiskHigh'},'message':'仅监控事件，无实际磁盘测量'})
    event=Event(protocol_version=2,event_key=event.event_key,category=category,evidence_hash='0'*64,evidence=[evidence],facts=[Fact(fact_id='F1',evidence_id='E1',path='labels.alertname',label='监控现象',role='alertname',value_type='text',value='PowerSystemDiskHigh',display_value='磁盘使用率监控告警（无测量数据）')])
   else:
    event.category=category
    if category=='device_alarm':
     value=event.evidence[1].data['points'][-1][1]
     event.evidence[0].data.update(metric='temperature',value=value,threshold=40)
     event.facts[0]=Fact(fact_id='F1',evidence_id='E1',path='value',label='触发温度',role='alarm_value',value_type='number',value=value,display_value=str(value),unit='℃')
   started=time.monotonic();result=await run_event(event,provider,AsyncMock(),probe=True)
   out.append({'case':index+1,'category':category,'status':result['status'],'reason':result['reason'],'error_stage':result['error_stage'],'elapsed_ms':round((time.monotonic()-started)*1000),'diagnostic':result['diagnostic']})
   print(json.dumps(out[-1]),flush=True)
   if result['reason'] in {'auth_failed','quota_exhausted','not_configured'}:break
 print(json.dumps({'passed':len(out)==(1 if SELECTED_CASE else 6) and all(row['status']=='answered' for row in out),'full_gate':not bool(SELECTED_CASE),'calls':len(out),'cases':out}))
asyncio.run(main())
'''
        probe="SELECTED_CASE="+repr(args.case)+"\n"+probe
        remote(preamble + f"""
batch=Path({BATCH!r}); source=batch/'candidate/stages/07-agent'
image=subprocess.check_output(['docker','inspect','--format','{{{{.Image}}}}','powersystem-stage4-agent-1'],text=True).strip()
cmd=['docker','run','--rm','-i','--label','com.docker.compose.project=powersystem-interpret-candidate','--label','com.docker.compose.oneoff=True','--network','bridge','-v',str(source/'agent')+':/app/agent:ro','-v','/etc/powersystem/interpret-deepseek-key:/run/secrets/event_key:ro','--entrypoint','python',image,'-']
try: p=subprocess.run(cmd,input={probe!r},capture_output=True,text=True,timeout=260)
except subprocess.TimeoutExpired as error:
 partial=(error.stdout or b'');partial=partial.decode() if isinstance(partial,bytes) else partial
 (batch/('official-gate-timeout-'+str(__import__('time').time_ns())+'.log')).write_text(partial)
 print(json.dumps({{'passed':False,'reason':'candidate_runner_timeout','partial_cases':len(partial.splitlines())}}));raise SystemExit(0)
if p.returncode: print(json.dumps({{'passed':False,'runner_exit':p.returncode,'error_types':__import__('re').findall(r'^([A-Za-z_.]+(?:Error|Exception|Expired)):',p.stderr,__import__('re').M)}}))
else:
 (batch/('official-gate-output-'+str(__import__('time').time_ns())+'.jsonl')).write_text(p.stdout)
 result=json.loads(p.stdout.strip().splitlines()[-1])
 import time
 (batch/('official-gate-'+str(time.time_ns())+'.json')).write_text(json.dumps(result));(batch/'official-gate.json').write_text(json.dumps(result));print(json.dumps(result))
""", timeout=280)


if __name__ == "__main__":
    main()
