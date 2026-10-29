#!/usr/bin/env python3
"""Read live queue volumes in a disposable, networkless fixed-image helper."""
import argparse,json,subprocess,re,signal
from pathlib import Path
ROOT=Path('/opt/powersystem')

def stop_owned_helper(name, image, output):
    ids=subprocess.check_output(['docker','ps','-aq','--filter','name=^/'+name+'$'],timeout=10).decode().split()
    if not ids:return
    if len(ids)!=1:raise ValueError('queue_helper_identity_ambiguous')
    row=json.loads(subprocess.check_output(['docker','inspect',ids[0]],timeout=10))[0]
    if (row['Image']!=image or row['Config']['Labels'].get('powersystem.backup.run')!=output.parent.name or
        row['HostConfig']['NetworkMode']!='none' or not any(m['Destination']=='/backup' and
        m['Source']==str(output.parent) for m in row['Mounts'])):
        raise ValueError('queue_helper_not_owned')
    subprocess.run(['docker','stop','--time','10',ids[0]],check=True,capture_output=True,timeout=25)
    if subprocess.check_output(['docker','ps','-q','--filter','id='+ids[0]],timeout=10).strip():
        raise ValueError('queue_helper_stop_unproven')
def capture(output):
    output=Path(output).resolve()
    if output.parent.parent!=Path('/var/backups/powersystem') or output.name!='queues' or output.exists():
        raise ValueError('owned_queue_snapshot_directory_required')
    if re.fullmatch(r'powersystem-\d{8}T\d{6}Z',output.parent.name) is None:
        raise ValueError('queue_backup_run_invalid')
    name='queue-snapshot-'+output.parent.name
    run=['docker','run','--rm','--pull','never','--name',name,'--label','powersystem.backup.run='+output.parent.name,
         '--cpus','1','--memory','512m','--network','none','--read-only','--cap-drop','ALL',
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
    try:
        result=subprocess.run(run,check=True,capture_output=True,timeout=660)
    finally:
        # A killed Docker CLI is not proof that its daemon-side helper stopped.
        try:stop_owned_helper(name,image,output)
        except Exception:
            (output.parent/'queue-helper-recovery-required.json').write_text(json.dumps({
                'name':name,'image':image,'output':str(output),'passed':False}))
            raise
    for name in ('gateway.sqlite','simulator.sqlite'):
        if not (output/name).is_file():raise ValueError('queue_snapshot_missing')
    metadata={'helper_image':image,'source_volumes':sources,'network':'none','source_readonly':True,
              'ephemeral_helper':True,'helper_name':name,'budget_seconds':600,
              'queues':[json.loads(line) for line in result.stdout.decode().splitlines() if line.startswith('{')]}
    (output.parent/'queue-helper.json').write_text(json.dumps(metadata,indent=2))
    print(json.dumps({'queues_saved':2,'fixed_helper_image':image,'source_readonly':True}))
if __name__=='__main__':
    def interrupted(_signum, _frame):raise InterruptedError('queue_snapshot_cancelled')
    signal.signal(signal.SIGTERM,interrupted)
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);capture(parser.parse_args().output)
