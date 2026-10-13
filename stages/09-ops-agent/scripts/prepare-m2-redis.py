"""Replace only this run's freshly-created, empty Redis fixture with a private config mount."""
import argparse
import json
import subprocess
from pathlib import Path
from redis import Redis


def main(args):
    root=Path(__file__).resolve().parents[3];run=(root/args.run).resolve()
    if not run.is_relative_to(root/'artifacts/stage9-ops-agent'):raise ValueError('invalid_run')
    docker=Path.home()/'AppData/Local/Programs/DockerDesktop/resources/bin/docker.exe'
    name='ma-m2-regression-redis'
    def execute(argv):
        r=subprocess.run([str(docker),*argv],capture_output=True,text=True)
        if r.returncode:raise ValueError('owned_fixture_command_failed')
        return r.stdout.strip()
    info=json.loads(execute(['inspect',name]))[0]
    if info['Id']!=args.expected_container_id or info['Config']['Labels'].get('powersystem.ma-milestone')!='MA-M2':raise ValueError('fixture_ownership_mismatch')
    if info['HostConfig']['PortBindings'].get('6379/tcp')!=[{'HostIp':'127.0.0.1','HostPort':'16378'}]:raise ValueError('fixture_port_mismatch')
    counts=[]
    for db in range(16):
        client=Redis(host='127.0.0.1',port=16378,db=db);counts.append(client.dbsize());client.close()
    if any(counts):raise ValueError('fixture_not_empty')
    (run/'tests'/'redis-initial-empty.json').write_text(json.dumps({'container_id':info['Id'],'key_counts':counts}),encoding='utf-8')
    execute(['rm','-f',info['Id']])
    cid=execute(['run','--detach','--name',name,'--label','powersystem.ma-milestone=MA-M2','--publish','127.0.0.1:16378:6379',
                 '--mount','type=bind,source='+str(run/'private/redis.conf')+',target=/usr/local/etc/redis/redis.conf,readonly',
                 'redis:7.4.5-alpine','redis-server','/usr/local/etc/redis/redis.conf'])
    (run/'tests'/'redis-fixture.json').write_text(json.dumps({'container_id':cid,'name':name,'port':16378,'password_required':True}),encoding='utf-8')
    print('Only this run\'s verified empty fixture replaced; Redis now loads its private authentication config.')


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--run',required=True);p.add_argument('--expected-container-id',required=True);main(p.parse_args())
