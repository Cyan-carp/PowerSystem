"""Thirty loopback MQTT/WebSocket regressions, including a full chat pool."""
import argparse
import csv
import importlib.util
import json
import os
import subprocess
import sys
import shutil
import time
from pathlib import Path
from uuid import uuid4
from redis import Redis
from agent.config import Config

ROOT=Path(__file__).resolve().parents[3]


def main(args):
    output=args.output;output.mkdir(parents=True,exist_ok=True)
    cfg=Config.load()
    if cfg.backend_url!='http://127.0.0.1:8080': raise ValueError('local acceptance only')
    shell=shutil.which('pwsh')
    if not shell:raise ValueError('PowerShell 7 required for UTF-8 script paths')
    import httpx
    def agent_alive():
        try:return httpx.get('http://127.0.0.1:8092/health',timeout=2,trust_env=False).status_code==200
        except httpx.RequestError:return False
    if not agent_alive():raise ValueError('normal mode requires a healthy Agent')
    spec=importlib.util.spec_from_file_location('smoke',ROOT/'stages/07-agent/scripts/smoke-v2m2.py')
    smoke=importlib.util.module_from_spec(spec);spec.loader.exec_module(smoke)
    redis=Redis.from_url(cfg.redis_url,decode_responses=True)
    admin=int(smoke.sql("SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1").splitlines()[0])
    admin_file=output/'admin-token.private'
    admin_file.write_text(smoke.jwt(admin),encoding='utf-8')
    lease_names=['v2m4-mainchain-'+uuid4().hex for _ in range(4)]
    rows=[]
    def save(): (output/'summary.json').write_text(json.dumps({'completed':len(rows)==30,'passed':len(rows)==30 and all(x['passed'] for x in rows),'cases':rows},indent=2),encoding='utf-8')
    def agent_start():
        with (output/'agent-restarted.log').open('a',encoding='utf-8') as log:
            subprocess.run([shell,'-NoProfile','-File',str(ROOT/'stages/07-agent/scripts/start-agent.ps1'),'-ConfigFile',str(args.config)],cwd=ROOT,check=True,stdout=log,stderr=subprocess.STDOUT)
        if not agent_alive():raise AssertionError('Agent not restored')
    stopped=False
    try:
        for mode in ('normal','agent-down','chat-full'):
            if mode=='agent-down':
                subprocess.run([shell,'-NoProfile','-File',str(ROOT/'stages/07-agent/scripts/stop-agent.ps1')],cwd=ROOT,check=True)
                stopped=True
                if agent_alive():raise AssertionError('Agent stop did not take effect')
            if mode=='chat-full':agent_start();stopped=False
            for i in range(10):
                if mode=='chat-full':
                    now=int(time.time()*1000)
                    redis.zadd('stage7:chat:leases',{name:now+50000 for name in lease_names})
                    redis.pexpire('stage7:chat:leases',51000)
                    import httpx
                    user=int(smoke.sql("SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1").splitlines()[0])
                    result=httpx.post('http://127.0.0.1:8080/api/v1/agent/chat',json={'message':'查询'},headers={'Authorization':'Bearer '+smoke.jwt(user)},timeout=5,trust_env=False)
                    if result.status_code!=429 or not result.headers.get('Retry-After'):raise AssertionError('full pool did not reject chat')
                target=output/f'{mode}-{i}.csv'
                with (output/f'{mode}-{i}.log').open('w',encoding='utf-8') as log:
                    result=subprocess.run([sys.executable,'-X','utf8',str(ROOT/'stages/02-backend/scripts/smoke-stage2.py'),'--admin-token-file',str(admin_file),'--output',str(target)],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT)
                cases=list(csv.DictReader(target.open(encoding='utf-8'))) if target.exists() else []
                timings=[float(row['detail'].removesuffix('s')) for row in cases if row['case']=='18a alarm latency within 10s']
                passed=result.returncode==0 and len(cases)>=38 and all(row['passed']=='True' for row in cases) and len(timings)==1 and timings[0]<=10
                rows.append({'mode':mode,'index':i,'passed':passed,'checks':len(cases),'alarm_ws_seconds':timings[0] if timings else None,'evidence':target.name})
                save()
                if not passed:raise AssertionError('business regression '+mode+' '+str(i))
                # Separate experiments after their recovery and cleanup, rather
                # than measuring an accidental overlapping local batch.
                time.sleep(3)
        return True
    finally:
        redis.zrem('stage7:chat:leases',*lease_names);redis.close()
        admin_file.unlink(missing_ok=True)
        if stopped:agent_start()
        save()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);parser.add_argument('--config',type=Path,required=True)
    raise SystemExit(0 if main(parser.parse_args()) else 1)
