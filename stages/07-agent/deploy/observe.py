"""Bounded read-only public/server observation; no model requests."""
import argparse
import json
import subprocess
import time
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime,timezone

def main(seconds, output=None):
    if not 1800<=seconds<=7200:raise ValueError('observation duration must be 30-120 minutes')
    output=output or Path('/opt/powersystem/runtime/v2m2-20261001/observation.json')
    samples=[];started=time.monotonic();start=datetime.now(timezone.utc).isoformat()
    def status(path):
        before=time.monotonic()
        try:
            with urllib.request.urlopen('https://8.138.10.222'+path,timeout=10) as response:code=response.status
        except urllib.error.HTTPError as error:code=error.code
        except OSError:code=0
        return {'code':code,'milliseconds':round((time.monotonic()-before)*1000)}
    while True:
        sample={'time':datetime.now(timezone.utc).isoformat(),'ping':status('/api/v1/ping'),'internal':status('/internal/agent/model-status'),'JWT':status('/api/v1/agent/interpretations')}
        raw=subprocess.check_output(['docker','inspect','--format','{{json .}}','powersystem-stage4-api-1','powersystem-stage4-agent-1','powersystem-stage4-frontend-1'],text=True)
        states=[json.loads(line) for line in raw.splitlines()]
        sample['services']=[{'running':state['State']['Running'],'health':state['State'].get('Health',{}).get('Status','none'),'restart_count':state['RestartCount'],'image':state['Image']} for state in states]
        sample['passed']=sample['ping']['code']==200 and sample['internal']['code']==403 and sample['JWT']['code']==401 and all(state['running'] and state['health']=='healthy' for state in sample['services'])
        samples.append(sample);elapsed=round(time.monotonic()-started,2);completed=elapsed>=seconds
        output.write_text(json.dumps({'started_at':start,'elapsed_seconds':elapsed,'duration_seconds':seconds,'completed':completed,'passed':completed and all(sample['passed'] for sample in samples),'samples':samples},ensure_ascii=False,indent=2))
        if completed:break
        time.sleep(min(30,seconds-elapsed))
    print('Observation complete:',len(samples),'samples; passed=',all(sample['passed'] for sample in samples))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--seconds',type=int,default=1800)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args();main(args.seconds,args.output)
