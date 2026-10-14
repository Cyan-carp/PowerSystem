"""Observe the human task card, then close all local write gates. Never approves."""
import argparse,asyncio,json,subprocess,time
from datetime import datetime,timezone
from pathlib import Path
import httpx
ROOT=Path(__file__).resolve().parents[3]
def reload_owned(run,ep,shell,label):
    if shell.name.lower()!='pwsh.exe' or not shell.is_file():raise ValueError('explicit_powershell7_runtime_required')
    command=[str(shell),'-NoProfile','-File',str(ROOT/'stages/10-security-agent/scripts/start-m4.ps1'),'-ConfigFile',str(ep),'-Frontend']
    # Native background children may inherit pipe handles. File diagnostics avoid
    # waiting for a pipe EOF after the launcher itself has already exited.
    with (run/(label+'.stdout.log')).open('xb') as out,(run/(label+'.stderr.log')).open('xb') as err:
        r=subprocess.run(command,cwd=ROOT,stdout=out,stderr=err,stdin=subprocess.DEVNULL,timeout=60)
    return r.returncode==0

async def main(a):
    run=(ROOT/a.run).resolve()
    if not run.is_relative_to(ROOT/'artifacts/stage10-security-agent') or not run.name.startswith('sec-m4-'):raise ValueError('owned_batch_required')
    private=run/'private';window=private/'broker/write-window.json';pending=run/'write-pending.json';reason='watchdog_failure'
    gate=json.loads(window.read_text());until=gate['window_until']
    if a.closed_reload:
        if gate['write_enabled'] or 'OPS_WRITE_ENABLED=true' in (private/'local.env').read_text():raise ValueError('closed_recovery_only')
        ready={'closed':True,'approval_performed_by_script':False,'owned_services_reloaded':reload_owned(run,private/'local.env',a.shell_executable,'watchdog-reload-recovery')}
        (run/'watchdog-reload-recovery.json').write_text(json.dumps(ready,indent=2));print(json.dumps(ready))
        if not ready['owned_services_reloaded']:raise ValueError('owned_reload_failed')
        return
    if not gate['write_enabled'] or not time.time()<until<=time.time()+1801:raise ValueError('bounded_open_window_required')
    env=dict(l.split('=',1) for l in (private/'local.env').read_text().splitlines() if '=' in l)
    if env['OPS_BACKEND_URL']!='http://127.0.0.1:18106':raise ValueError('owned_local_required')
    try:
        async with httpx.AsyncClient(trust_env=False,timeout=5) as c:
            r=await c.post(env['OPS_BACKEND_URL']+'/api/v1/auth/login',json={'username':'ma_m3_admin','password':Path(env['MA_M3_ADMIN_PASSWORD_FILE']).read_text().strip()});r.raise_for_status();headers={'Authorization':'Bearer '+r.json()['data']['token']}
            while time.time()<until:
                if pending.exists():
                    tid=json.loads(pending.read_text(encoding='utf-8-sig'))['task_id']
                    r=await c.get(env['OPS_BACKEND_URL']+'/api/v1/agent/tasks/'+tid,headers=headers);r.raise_for_status();report=r.json()['data']
                    if report['status'] in ('succeeded','failed','cancelled','timeout'):
                        reason='terminal_'+report['status'];(run/'write-terminal.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));break
                await asyncio.sleep(1)
            else:reason='window_expired'
    finally:
        # The authoritative broker reloads this file before every restart.
        window.write_text(json.dumps({'write_enabled':False,'window_until':until},indent=2))
        bp=private/'broker/broker.json';value=json.loads(bp.read_text());value['write_enabled']=False;bp.write_text(json.dumps(value,indent=2))
        ep=private/'local.env';ep.write_text(ep.read_text().replace('OPS_WRITE_ENABLED=true','OPS_WRITE_ENABLED=false'))
        ready={'closed':True,'reason':reason,'closed_at':datetime.now(timezone.utc).isoformat(),'window_until':until,'approval_performed_by_script':False}
        (run/'watchdog-closed.json').write_text(json.dumps(ready,indent=2))
        ready['owned_services_reloaded']=reload_owned(run,ep,a.shell_executable,'watchdog-owned-reload')
        (run/'watchdog-closed.json').write_text(json.dumps(ready,indent=2));print(json.dumps(ready))
        if not ready['owned_services_reloaded']:raise ValueError('closed_but_owned_reload_failed')

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--run',required=True);p.add_argument('--shell-executable',type=Path,required=True);p.add_argument('--closed-reload',action='store_true');asyncio.run(main(p.parse_args()))
