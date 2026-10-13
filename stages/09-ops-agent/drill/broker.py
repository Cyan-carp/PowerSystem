"""Model-free local broker. Only a registered drill ID can be inspected/restarted."""
import json
import os
import sqlite3
import time
from datetime import datetime,timezone
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from collector.broker import Docker, UnixHTTP
from ops_contract import digest,canonical,validate_dispatch
from ops_write_contract import verify,verify_snapshot,matches,proof


class Broker:
    def __init__(self,config,docker=None):
        self.config=config;self.registry=config['registry'];self.key=Path(config['write_key_file']).read_text(encoding='utf-8-sig').strip()
        self.read_key=Path(config['read_key_file']).read_text(encoding='utf-8-sig').strip()
        if min(len(self.key),len(self.read_key))<32: raise ValueError('broker_credentials_required')
        self.docker=docker or Docker()
        self.state=Path(config['state_file']);self.state.parent.mkdir(parents=True,exist_ok=True)
        with sqlite3.connect(self.state) as db:
            db.execute('CREATE TABLE IF NOT EXISTS attempts(approval_id TEXT PRIMARY KEY, call_id TEXT UNIQUE, started_at REAL NOT NULL)')

    def snapshot(self,aid):
        cfg=self.config
        if aid!=cfg['asset_id']: raise ValueError('asset_not_found')
        info=self.docker.get(self.docker.version+'/containers/'+cfg['container_id']+'/json')
        labels=info.get('Config',{}).get('Labels') or {};hc=info['HostConfig'];cc=info['Config']
        expected=cfg['policy']
        milestone=expected.get('milestone','MA-M3')
        if milestone not in ('MA-M3','SEC-M2'): raise ValueError('guard_blocked')
        env={v.split('=',1)[0] for v in cc.get('Env') or []}
        stateless=(not info.get('Mounts') and hc.get('ReadonlyRootfs') is True and not hc.get('Privileged') and
                   hc.get('RestartPolicy',{}).get('Name') in ('no','') and 'ALL' in (hc.get('CapDrop') or []) and
                   'no-new-privileges' in ' '.join(hc.get('SecurityOpt') or []) and cc.get('User')=='65532:65532' and
                   hc.get('NetworkMode')==expected['network'] and bool(cc.get('Healthcheck',{}).get('Test')) and
                   env<={'PATH','LANG','GPG_KEY','PYTHON_VERSION','PYTHON_SHA256','PYTHONUNBUFFERED','PYTHONDONTWRITEBYTECODE'})
        if (info['Id']!=cfg['container_id'] or info['Image']!=cfg['image_digest'] or
            labels.get('com.docker.compose.project')!=expected['project'] or labels.get('com.docker.compose.service')!='drill' or
            labels.get('powersystem.milestone')!=milestone or not stateless): raise ValueError('asset_version_changed')
        safety={'container_id':info['Id'],'image_digest':info['Image'],'labels':labels,'readonly':hc['ReadonlyRootfs'],
                'mounts':info['Mounts'],'network':hc['NetworkMode'],'user':cc['User'],'caps':hc['CapDrop'],
                'security_options':hc['SecurityOpt'],'restart_policy':hc['RestartPolicy'],'healthcheck':cc['Healthcheck'],
                'command':cc['Cmd'],'entrypoint':cc['Entrypoint'],'environment':cc.get('Env')}
        a=next(a for a in self.registry['assets'] if a['asset_id']==aid)
        state=info['State']
        return proof({'asset_id':aid,'asset_version':a['version'],'registry_hash':digest(self.registry['assets']),
            'container_id':info['Id'],'image_digest':info['Image'],'config_hash':digest(safety),
            'policy_version':digest(expected),'state':state['Status'],'running':state['Running'],
            'health':state.get('Health',{}).get('Status','not_configured'),'started_at':state['StartedAt'],
            'stateless_verified':True,'collected_at':datetime.now(timezone.utc).isoformat()})

    def restart(self,token):
        g=verify(token,self.key)
        if g['action']!='restart': raise ValueError('guard_blocked')
        gate=json.loads(Path(self.config['window_file']).read_text(encoding='utf-8-sig')) if self.config.get('window_file') else self.config
        if gate.get('write_enabled') is not True or type(gate.get('window_until')) not in (int,float) or not __import__('math').isfinite(gate['window_until']) or gate['window_until']>self.config['window_until'] or time.time()>=gate['window_until']: raise ValueError('write_disabled')
        before=self.snapshot(g['asset_id'])
        if not matches(g,before) or before['health']!='healthy' or not before['running'] or g['args_hash']!=digest({'asset_id':g['asset_id']}): raise ValueError('approval_invalidated')
        # Committed before sending the Docker request. Lost results never authorize a replay.
        try:
            with sqlite3.connect(self.state,timeout=2) as db:
                db.execute('INSERT INTO attempts VALUES(?,?,?)',(g['approval_id'],g['call_id'],time.time()))
        except sqlite3.IntegrityError: raise ValueError('dispatch_replayed') from None
        try:
            remaining=min(15,g['deadline']-time.time());until=time.monotonic()+remaining
            c=UnixHTTP('localhost',timeout=max(.01,min(11,remaining)))
            try:
                c.request('POST',self.docker.version+'/containers/'+g['container_id']+'/restart?t=10',body=b'')
                r=c.getresponse();r.read(4096)
                if r.status!=204: raise ValueError('execution_uncertain')
            finally:c.close()
            while time.monotonic()<until:
                after=self.snapshot(g['asset_id'])
                if matches(before,after) and after['running'] and after['health']=='healthy' and after['started_at']!=before['started_at']:
                    return {'status':'ok','before':before,'after':after,'action_confirmed':True}
                time.sleep(.25)
            raise ValueError('execution_uncertain')
        except Exception: raise ValueError('execution_uncertain') from None


def serve(config):
    broker=Broker(config)
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path!='/health':self.send_error(404);return
            self.reply(200,{'status':'ok','engine':'isolated-drill-broker'})
        def reply(self,status,value):
            body=canonical(value);self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        def do_POST(self):
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=16384:raise ValueError('invalid_args')
                value=json.loads(self.rfile.read(size))
                if self.path=='/internal/drill/v1/snapshot' and set(value)=={'grant'}:
                    aid=verify_snapshot(value['grant'],broker.key)
                    self.reply(200,{'snapshot':broker.snapshot(aid),'policy':config['policy']});return
                if self.path=='/internal/drill/v1/restart' and set(value)=={'grant'}:
                    self.reply(200,broker.restart(value['grant']));return
                if self.path=='/internal/drill/v1/read':
                    g,args,a=validate_dispatch(value,broker.read_key,broker.registry)
                    if value['tool']!='docker.inspect_container' or args['asset_id']!=config['asset_id']:raise ValueError('guard_blocked')
                    s=broker.snapshot(args['asset_id'])
                    info=broker.docker.get(broker.docker.version+'/containers/'+config['container_id']+'/json')
                    from collector.broker import container_view
                    data={**container_view(info),'sampled_at':s['collected_at'],'collected_at':s['collected_at'],'truncated':False,'complete':True}
                    self.reply(200,{'status':'ok','data':data,'error':None});return
                raise ValueError('guard_blocked')
            except Exception as e:
                code=str(e) if str(e) in ('execution_uncertain','dispatch_replayed','write_disabled','approval_invalidated','asset_version_changed','asset_not_found','guard_blocked','invalid_args','invalid_write_grant') else 'broker_unavailable'
                self.reply(409 if code=='dispatch_replayed' else 403 if code!='execution_uncertain' else 503,{'detail':code})
        def log_message(self,*args):pass
    ThreadingHTTPServer(('0.0.0.0',8090),Handler).serve_forever()


if __name__=='__main__':serve(json.loads(Path(os.environ['DRILL_CONFIG_FILE']).read_text(encoding='utf-8-sig')))
