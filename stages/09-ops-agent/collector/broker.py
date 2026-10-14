"""Fixed SSH entry. No shell, Docker management, arbitrary SQL or raw-log output."""
import hashlib
import http.client
import json
import os
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from ops_contract import canonical, digest, safe_logs, validate_dispatch

ROOT = Path('/etc/powersystem-ops-read')


class UnixHTTP(http.client.HTTPConnection):
    def connect(self):
        self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect('/var/run/docker.sock')


class Docker:
    def __init__(self):
        info=self.get('/version')
        version=info.get('ApiVersion','')
        if not __import__('re').fullmatch(r'1\.[0-9]{2,3}',version): raise ValueError('capability_unavailable')
        self.version='/v'+version

    def get(self,path,params=None,raw=False):
        if not (path=='/version' or path.startswith('/v1.')): raise ValueError('guard_blocked')
        c=UnixHTTP('localhost',timeout=8)
        try:
            c.request('GET',path+('?' + urlencode(params) if params else ''))
            r=c.getresponse()
            if r.status==404: raise ValueError('asset_not_found')
            if r.status!=200: raise ValueError('target_unreachable')
            data=r.read(262145)
            if raw: return data[:262144],len(data)>262144
            if len(data)>262144: raise ValueError('invalid_result')
            return json.loads(data)
        finally: c.close()

    def inspect(self,private):
        cid=private['container_id']
        if not __import__('re').fullmatch('[a-f0-9]{64}',cid): raise ValueError('asset_not_verified')
        info=self.get(self.version+'/containers/'+cid+'/json')
        labels=info.get('Config',{}).get('Labels') or {}
        identity={'container_id':info['Id'],'image_id':info['Image'],
                  'project':labels.get('com.docker.compose.project'), 'service':labels.get('com.docker.compose.service')}
        if identity!={k:private[k] for k in identity}: raise ValueError('asset_version_changed')
        return info


def container_view(info):
    state=info['State']
    health=state.get('Health',{}).get('Status','not_configured')
    return {'state':state['Status'],'running':state['Running'],'restart_count':info['RestartCount'],
            'image_digest':info['Image'],'health':health,'started_at':state.get('StartedAt'),
            'finished_at':state.get('FinishedAt'),'exit_code':state.get('ExitCode'), 'vantage':'container'}


def cpu_snapshot():
    values=[int(x) for x in Path('/proc/stat').read_text().splitlines()[0].split()[1:9]]
    return sum(values),values[3]+values[4]


def fixed_process(argv,limit=262144):
    """Bound stdout/stderr while reading; child never inherits caller environment or descriptors."""
    import selectors
    p=subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                       env={'PATH':'/usr/bin:/bin','LC_ALL':'C'},close_fds=True)
    collected=bytearray();truncated=False
    try:
        selector=selectors.DefaultSelector();selector.register(p.stdout,selectors.EVENT_READ)
        until=time.monotonic()+8
        while selector.get_map():
            if time.monotonic()>=until: raise TimeoutError()
            for key,_ in selector.select(.2):
                block=os.read(key.fileobj.fileno(),8192)
                if not block: selector.unregister(key.fileobj);continue
                remaining=max(0,limit-len(collected));collected.extend(block[:remaining])
                if len(block)>remaining: truncated=True
        if p.wait(timeout=1)!=0: raise ValueError('capability_unavailable')
        return collected.decode('utf-8','replace'),truncated
    finally:
        if p.poll() is None: p.kill();p.wait()
        p.stdout.close()


def unmultiplex(raw):
    out=bytearray();pos=0
    while pos+8<=len(raw) and raw[pos] in (0,1,2) and raw[pos+1:pos+4]==b'\0\0\0':
        size=struct.unpack('>I',raw[pos+4:pos+8])[0]
        out.extend(raw[pos+8:min(pos+8+size,len(raw))]);pos+=8+size
    return (bytes(out) if pos else raw).decode('utf-8','replace')


class Collector:
    def __init__(self,registry,docker_factory=Docker):
        self.registry=registry;self.assets={a['asset_id']:a for a in registry['assets']}
        self.private=registry['private_assets'];self.docker_factory=docker_factory;self._docker=None

    @property
    def docker(self):
        if self._docker is None: self._docker=self.docker_factory()
        return self._docker

    def private_asset(self,asset):
        p=self.private.get(asset['asset_id'])
        if not isinstance(p,dict): raise ValueError('asset_not_verified')
        identity=p.get('identity')
        if not isinstance(identity,dict) or digest(identity)!=asset['identity_hash']: raise ValueError('asset_version_changed')
        host=asset if asset['kind']=='host' else self.assets[asset['parent_id']]
        hp=self.private[host['asset_id']]['identity']
        actual=hashlib.sha256(Path('/etc/machine-id').read_bytes().strip()).hexdigest()
        if hp.get('machine_hash')!=actual or hp.get('project')!='powersystem-stage4': raise ValueError('asset_version_changed')
        return p

    def children(self,asset,kind):
        return [a for a in self.registry['assets'] if a['parent_id']==asset['asset_id'] and a['kind']==kind and a['enabled'] and a['verified_at']]

    def collect(self,name,args,asset):
        p=self.private_asset(asset);identity=p['identity']
        stamp=datetime.now(timezone.utc).isoformat()
        data={'vantage':'host','sampled_at':stamp,'collected_at':stamp,'truncated':False,'complete':True}
        if name=='linux.get_cpu_usage':
            first=cpu_snapshot();time.sleep(1);last=cpu_snapshot();delta=last[0]-first[0]
            data.update(cpu_percent=round(100*(1-(last[1]-first[1])/delta),3) if delta>0 else None,
                        load_1m=os.getloadavg()[0],logical_cpus=os.cpu_count(),sample_seconds=1)
        elif name=='linux.get_memory_usage':
            values={k:int(v.split()[0])*1024 for k,v in (line.split(':',1) for line in Path('/proc/meminfo').read_text().splitlines())}
            data.update(total_bytes=values['MemTotal'],available_bytes=values['MemAvailable'],
                        used_bytes=values['MemTotal']-values['MemAvailable'],swap_total_bytes=values['SwapTotal'],
                        swap_free_bytes=values['SwapFree'])
        elif name=='linux.get_disk_status':
            items=[]
            for mount in p['mounts']:
                s=os.statvfs(mount['path'])
                items.append({'filesystem':mount['alias'],'total_bytes':s.f_blocks*s.f_frsize,
                              'available_bytes':s.f_bavail*s.f_frsize,'free_bytes':s.f_bfree*s.f_frsize})
            data['filesystems']=items
        elif name=='linux.list_services':
            rows=[]
            for child in self.children(asset,'service'):
                cp=self.private_asset(child);unit=cp['identity']['unit']
                info=self.service(unit,cp['identity']['fragment_hash'])
                rows.append({'asset_id':child['asset_id'],**info})
            data['list']=rows
        elif name=='linux.get_system_logs':
            unit=identity['unit'];self.service(unit,identity['fragment_hash'])
            text,truncated=fixed_process(['/usr/bin/journalctl','--no-pager','--output=json','--unit',unit,
                                        '--lines',str(args['limit']),'--since','-'+str(args['since_seconds'])+' seconds'])
            parsed=[]
            for line in text.splitlines():
                try:
                    row=json.loads(line);message=json.loads(row.get('MESSAGE',''))
                    if isinstance(message,dict): parsed.append(json.dumps(message))
                    else: parsed.append('{}')
                except (ValueError,TypeError): parsed.append('{}')
            data.update(safe_logs('\n'.join(parsed),args['limit'],truncated))
        elif name=='docker.list_containers':
            data['list']=[]
            for child in self.children(asset,'container'):
                cp=self.private_asset(child)
                data['list'].append({'asset_id':child['asset_id'],**container_view(self.docker.inspect(cp['identity']))})
        elif name.startswith('docker.'):
            info=self.docker.inspect(identity);data['vantage']='container'
            cid=identity['container_id']
            if name=='docker.inspect_container': data.update(container_view(info))
            elif name=='docker.get_container_stats':
                first=self.docker.get(self.docker.version+'/containers/'+cid+'/stats',{'stream':'false','one-shot':'true'})
                time.sleep(1)
                last=self.docker.get(self.docker.version+'/containers/'+cid+'/stats',{'stream':'false','one-shot':'true'})
                cpu=last['cpu_stats'];prev=first['cpu_stats']
                delta=cpu['cpu_usage']['total_usage']-prev['cpu_usage']['total_usage']
                system=cpu.get('system_cpu_usage',0)-prev.get('system_cpu_usage',0)
                memory=last['memory_stats'];usage=memory.get('usage',0)
                data.update(cpu_percent=round(delta/system*cpu.get('online_cpus',1)*100,3) if system>0 else None,
                            memory_usage_bytes=usage,memory_limit_bytes=memory.get('limit',0),
                            memory_working_set_bytes=max(0,usage-memory.get('stats',{}).get('inactive_file',0)),sample_seconds=1)
            else:
                raw,truncated=self.docker.get(self.docker.version+'/containers/'+cid+'/logs',
                    {'stdout':'true','stderr':'true','timestamps':'false','tail':str(args['limit']),
                     'since':str(int(time.time())-args['since_seconds'])},raw=True)
                data.update(safe_logs(unmultiplex(raw),args['limit'],truncated))
        elif name.startswith('postgresql.'):
            data['vantage']='database'
            pgcontainer=p['postgres_container_asset']
            cp=self.private_asset(self.assets[pgcontainer]);info=self.docker.inspect(cp['identity'])
            network=p['network'];address=info['NetworkSettings']['Networks'][network]['IPAddress']
            import psycopg
            password=Path(p['password_file']).read_text().strip()
            # No caller-supplied SQL, database, host, user or transaction mode.
            sql='SELECT ma_ops_monitor.db_status()' if name.endswith('get_db_status') else 'SELECT ma_ops_monitor.connection_stats()'
            with psycopg.connect(host=address,port=5432,dbname=identity['database'],user='ma_ops_diagnostics',password=password,
                                 connect_timeout=4,options='-c default_transaction_read_only=on -c statement_timeout=5000') as conn:
                row=conn.execute(sql).fetchone()[0]
                if not isinstance(row,dict): raise ValueError('invalid_result')
                data.update(row)
        else: raise ValueError('guard_blocked')
        data['collected_at']=datetime.now(timezone.utc).isoformat()
        return data

    def service(self,unit,fragment_hash):
        if not __import__('re').fullmatch(r'[a-zA-Z0-9_.@-]+\.service',unit): raise ValueError('asset_not_verified')
        text,_=fixed_process(['/usr/bin/systemctl','show',unit,'--property=ActiveState,SubState,FragmentPath,LoadState'])
        fields=dict(line.split('=',1) for line in text.splitlines() if '=' in line)
        fragment=fields.get('FragmentPath')
        if not fragment or hashlib.sha256(Path(fragment).read_bytes()).hexdigest()!=fragment_hash: raise ValueError('asset_version_changed')
        return {k:fields.get(k) for k in ('ActiveState','SubState','LoadState')}


def consume_nonce(call_id,expires):
    with sqlite3.connect(ROOT/'calls.sqlite',timeout=2) as c:
        c.execute('CREATE TABLE IF NOT EXISTS consumed(call_id TEXT PRIMARY KEY, expires INTEGER NOT NULL)')
        c.execute('DELETE FROM consumed WHERE expires<?',(int(time.time())-60,))
        try: c.execute('INSERT INTO consumed VALUES(?,?)',(call_id,expires))
        except sqlite3.IntegrityError: raise ValueError('dispatch_replayed') from None


def main():
    def timeout(*_): raise TimeoutError()
    signal.signal(signal.SIGALRM,timeout);signal.alarm(15)
    try:
        if len(sys.argv)!=1 or os.environ.get('SSH_ORIGINAL_COMMAND'): raise ValueError('guard_blocked')
        raw=sys.stdin.buffer.read(65537)
        if len(raw)>65536: raise ValueError('invalid_dispatch')
        registry=json.loads((ROOT/'registry.json').read_text())
        request=json.loads(raw);key=(ROOT/'grant-key').read_text().strip()
        grant,args,asset=validate_dispatch(request,key,registry)
        consume_nonce(grant['call_id'],grant['exp'])
        signal.alarm(max(1,min(15,int(grant['deadline']-time.time()))))
        data=Collector(registry).collect(request['tool'],args,asset)
        reply={'status':'ok','data':data,'error':None}
    except Exception as exc:
        allowed={'guard_blocked','invalid_args','invalid_dispatch','invalid_execution_grant','context_mismatch','asset_version_changed',
                 'asset_not_found','asset_not_verified','capability_unavailable','dispatch_replayed','target_unreachable','invalid_result'}
        code=str(exc) if str(exc) in allowed else 'timeout' if isinstance(exc,TimeoutError) else 'collection_failed'
        reply={'status':'error','data':{},'error':{'code':code,'message':code,'retryable':code in ('target_unreachable','timeout')}}
    signal.alarm(0)
    sys.stdout.buffer.write(canonical(reply)+b'\n')


if __name__=='__main__': main()
