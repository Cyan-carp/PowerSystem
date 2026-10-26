#!/usr/bin/env python3
"""Read live queue volumes in a disposable, networkless fixed-image helper."""
import argparse,json,subprocess
from pathlib import Path
ROOT=Path('/opt/powersystem')
def capture(output):
    output=Path(output).resolve()
    if output.parent.parent!=Path('/var/backups/powersystem') or output.name!='queues' or output.exists():
        raise ValueError('owned_queue_snapshot_directory_required')
    run=['docker','run','--rm','--network','none','--read-only','--cap-drop','ALL',
         '--security-opt','no-new-privileges:true','--user','0:0','--entrypoint','python']
    sources={}
    for service in ('gateway','simulator'):
        row=json.loads(subprocess.check_output(['docker','inspect','powersystem-stage4-'+service+'-1']))[0]
        labels=row['Config']['Labels']
        if labels.get('com.docker.compose.project')!='powersystem-stage4' or labels.get('com.docker.compose.service')!=service:
            raise ValueError('owned_queue_writer_required')
        mounts=[m for m in row['Mounts'] if m['Destination']=='/data' and m['Type']=='volume']
        if len(mounts)!=1:raise ValueError('queue_data_volume_required')
        volume=mounts[0]['Name'];sources[service]=volume
        run+=['--mount','type=volume,source='+volume+',target=/source/'+service+',readonly']
    image=json.loads(subprocess.check_output(['docker','inspect','powersystem-stage4-ops-agent-1']))[0]['Image']
    run+=['--mount','type=bind,source='+str(output.parent)+',target=/backup',
          '--mount','type=bind,source='+str(ROOT/'stages/04-frontend/deploy/snapshot_queues.py')+',target=/snapshot.py,readonly',
          image,'/snapshot.py','/backup/queues']
    subprocess.run(run,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=180)
    for name in ('gateway.sqlite','simulator.sqlite'):
        if not (output/name).is_file():raise ValueError('queue_snapshot_missing')
    metadata={'helper_image':image,'source_volumes':sources,'network':'none','source_readonly':True,'ephemeral_helper':True}
    (output.parent/'queue-helper.json').write_text(json.dumps(metadata,indent=2))
    print(json.dumps({'queues_saved':2,'fixed_helper_image':image,'source_readonly':True}))
if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);capture(parser.parse_args().output)
