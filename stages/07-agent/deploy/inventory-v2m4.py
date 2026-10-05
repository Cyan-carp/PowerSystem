"""Read-only host ownership inventory without environment or token contents."""
import argparse
import json
import pathlib
import stat
import subprocess
import tomllib


def main(args):
    def command(values):
        result=subprocess.run(values,text=True,capture_output=True)
        return {'exit':result.returncode,'output':result.stdout.strip()}
    cfg=dict(line.split('=',1) for line in pathlib.Path('/opt/powersystem/.env').read_text().splitlines() if '=' in line and not line.startswith('#'))
    files=[pathlib.Path('/opt/powersystem/.env'),pathlib.Path('/etc/powersystem/backup-passphrase')]
    files += [pathlib.Path(value) for key,value in cfg.items() if key.endswith('_FILE')]
    permissions=[]
    for path in sorted(set(files)):
        permissions.append({'path':str(path),'mode':oct(stat.S_IMODE(path.stat().st_mode)),'uid':path.stat().st_uid,'gid':path.stat().st_gid})
    cfg=tomllib.loads(pathlib.Path('/opt/frp/frpc.toml').read_text())
    mappings=[{key:item.get(key) for key in ('name','type','localIP','localPort','remotePort')} for item in cfg.get('proxies',[])]
    names=['powersystem-stage4-'+s+'-1' for s in ('api','agent','frontend','postgres','redis','tdengine','emqx','ai-predict','prometheus','grafana','feishu-adapter')]
    raw=json.loads(subprocess.check_output(['docker','inspect',*names],text=True))
    containers=[{'name':x['Name'],'image':x['Image'],'privileged':x['HostConfig']['Privileged'],
        'ports':x['HostConfig']['PortBindings'],'network':x['HostConfig']['NetworkMode'],
        'mounts':[{'destination':m['Destination'],'read_write':m['RW'],'type':m['Type']} for m in x['Mounts']]} for x in raw]
    report={'completed':True,'mappings':mappings,'permissions':permissions,'containers':containers,
        'listening':command(['ss','-H','-lnt']), 'ipv4':command(['iptables','-S']), 'ipv6':command(['ip6tables','-S'])}
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,indent=2))
    print(json.dumps({'completed':True,'container_count':len(containers),'mapping_count':len(mappings),'privileged_count':sum(x['privileged'] for x in containers)}))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=pathlib.Path,required=True);main(p.parse_args())
