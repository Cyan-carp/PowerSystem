"""One observer, fixed six images, no restarts, 30s samples for >=1800s."""
import argparse,json,os,subprocess,time,urllib.request,urllib.error
from pathlib import Path
SERVICES=('api','frontend','agent-rag','ops-agent','security-agent','ops-executor')
def run(argv):
    p=subprocess.run(argv,capture_output=True)
    if p.returncode:raise RuntimeError('observation_probe_failed')
    return p.stdout
def inventory():
    ids=run(['docker','ps','-aq','--filter','label=com.docker.compose.project=powersystem-stage4']).decode().split()
    if not ids:raise ValueError('business_project_missing')
    rows=json.loads(run(['docker','inspect',*ids]))
    return {r['Config']['Labels']['com.docker.compose.service']:r for r in rows}

def states():
    rows=inventory()
    return {s:{'image':r['Image'],'running':r['State']['Running'],'health':r['State'].get('Health',{}).get('Status'),'restarts':r['RestartCount'],'started_at':r['State']['StartedAt']} for s,r in rows.items()}

def candidate_healthy(baseline,pins):
    return all(s in baseline and baseline[s]['image']==pins[s] and baseline[s]['running'] and baseline[s]['health']=='healthy' for s in SERVICES)

def business_healthy(baseline):
    # Existing workers may have no Docker healthcheck; running is still mandatory.
    return all(v['running'] and v['health'] in (None,'healthy') for v in baseline.values())

def metrics():
    text=run(['docker','exec','powersystem-stage4-postgres-1','psql','-U','powersystem','-d','ma_ops','-Atqc',"SELECT json_build_object('active',(SELECT count(*) FROM ma_tasks WHERE status IN ('queued','planning','executing')),'outbox_pending',(SELECT count(*) FROM ma_outbox WHERE NOT delivered),'review_pending',(SELECT count(*) FROM ma_security_reviews WHERE state='pending'),'uncertain',(SELECT count(*) FROM ma_execution_attempts WHERE status='execution_uncertain'),'oldest_outbox_seconds',(SELECT coalesce(extract(epoch FROM now()-min(t.created_at)),0) FROM ma_outbox o JOIN ma_tasks t USING(task_id) WHERE NOT o.delivered),'oldest_review_seconds',(SELECT coalesce(extract(epoch FROM now()-min(created_at)),0) FROM ma_security_reviews WHERE state='pending'))"]).decode()
    return json.loads(text)
def http(url):
    try:
        with urllib.request.urlopen(url,timeout=10) as r:return r.status
    except urllib.error.HTTPError as e:return e.code
    except OSError:return 0
def main(a):
    if os.uname().nodename!='R730XD' or a.seconds<1800:raise ValueError('specified_host_and_full_window_required')
    r=a.release.resolve();output=r/'observation-final.json'
    if not r.is_relative_to(Path('/opt/powersystem/runtime')) or output.exists():raise ValueError('new_observation_required')
    fd=os.open(r/'observation.lock',os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
    baseline=states();pins=json.loads((r/'candidate-images.json').read_text())
    if not candidate_healthy(baseline,pins) or not business_healthy(baseline):raise ValueError('candidate_or_original_business_not_healthy')
    report={'original_business_services':sorted(set(baseline)-set(SERVICES)),'baseline':baseline,'seconds':a.seconds,'samples':[],'completed':False,'passed':False};start=time.monotonic()
    while True:
        sample={'elapsed_seconds':round(time.monotonic()-start,3),'states':states(),'metrics':metrics(),'ping':http(a.url+'/api/v1/ping'),'internal':http(a.url+'/internal/ma-review/v1/reviews'),'jwt':http(a.url+'/api/v1/devices')}
        sample['passed']=sample['states']==baseline and sample['ping']==200 and sample['internal']==403 and sample['jwt']==401 and sample['metrics']['outbox_pending']<=20 and sample['metrics']['oldest_outbox_seconds']<=300 and sample['metrics']['review_pending']<=2 and sample['metrics']['oldest_review_seconds']<=30 and sample['metrics']['uncertain']==0
        report['samples'].append(sample)
        if time.monotonic()-start>=a.seconds:
            report['completed']=True;report['passed']=all(x['passed'] for x in report['samples']);report['elapsed_seconds']=time.monotonic()-start
        tmp=output.with_suffix('.tmp');tmp.write_text(json.dumps(report,indent=2));tmp.replace(output)
        if report['completed']:break
        time.sleep(min(30,max(0,a.seconds-(time.monotonic()-start))))
    print(json.dumps({'passed':report['passed'],'samples':len(report['samples']),'elapsed_seconds':report['elapsed_seconds']}))
    if not report['passed']:raise ValueError('observation_failed')
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--release',type=Path,required=True);p.add_argument('--url',default='https://8.138.42.212');p.add_argument('--seconds',type=int,default=1800);main(p.parse_args())
