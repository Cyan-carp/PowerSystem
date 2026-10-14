"""Loopback-only MA-C10 browser fault fixture; never a runtime service.

Serves an independently built, unmodified frontend and synthetic task responses.
No forwarding, models, databases, Redis, or business-service mutations. A real
socket reset exercises browser fetch failures. Control uses a private local key.
"""
import argparse
import copy
import hashlib
import hmac
import json
import mimetypes
import secrets
import socket
import struct
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, unquote
from urllib.request import Request, urlopen
from urllib.request import build_opener, ProxyHandler
from http.cookies import SimpleCookie
from uuid import uuid4


def stamp():
    return datetime.now(timezone.utc).isoformat()


class Fixture:
    def __init__(self, run, static_root):
        self.run, self.static_root = run, static_root
        private = run / 'private'
        private.mkdir(exist_ok=True)
        key_file = private / 'c10-control-key'
        if not key_file.exists(): key_file.write_text(secrets.token_urlsafe(40), encoding='utf8')
        self.key = key_file.read_text(encoding='utf8').strip()
        self.lock = threading.RLock()
        self.mode, self.scenario, self.retry_after, self.plain, self.latency_ms = 'normal', 'initial', '8', False, 0
        self.task = dict(protocol='ma/v1', task_id=str(uuid4()), trace_id=str(uuid4()), session_id=str(uuid4()),
            parent_turn_id=None, intent='MA-C10隔离本机故障注入 · 合成任务，不调用业务服务', status='executing',
            steps=[], calls=[], evidence_refs=[], diagnosis={'summary':'隔离轮询快照 A（合成任务）','hypotheses':[],'recommendations':[]},
            warnings=[], error=None, tool_calls=[], sources=[], verification_results=None, device=None,
            model_calls=0, link_pending=True, evidence={'state':'not_queried','success_count':0,'failed_count':0,'cards':[]},
            created_at=stamp(), finished_at=None)
        self.record('fixture_start', task_id=self.task['task_id'], external_calls=0)

    def record(self, event, **values):
        with self.lock:
            data = dict(at=stamp(), monotonic=time.monotonic(), event=event, scenario=self.scenario, **values)
            with (self.run/'requests.jsonl').open('a', encoding='utf8') as out:
                out.write(json.dumps(data, ensure_ascii=False)+'\n')

    def change(self, body):
        if set(body) - {'mode','scenario','retry_after','plain','snapshot','status','latency_ms'}: raise ValueError('unknown_control_field')
        if body.get('mode', 'normal') not in ('normal','rate','reset'): raise ValueError('invalid_mode')
        if body.get('status', 'executing') not in ('queued','planning','executing','succeeded','failed','cancelled','timeout'): raise ValueError('invalid_status')
        if not isinstance(body.get('scenario',''), str) or len(body.get('scenario',''))>80: raise ValueError('invalid_scenario')
        if not isinstance(body.get('retry_after','8'),str) or len(body.get('retry_after','8'))>80: raise ValueError('invalid_retry_after')
        if type(body.get('plain',False)) is not bool or type(body.get('latency_ms',0)) is not int or not 0<=body.get('latency_ms',0)<=20000: raise ValueError('invalid_control')
        with self.lock:
            self.mode = body.get('mode','normal'); self.scenario=body.get('scenario','manual')
            self.retry_after=body.get('retry_after','8'); self.plain=body.get('plain',False);self.latency_ms=body.get('latency_ms',0)
            if 'snapshot' in body: self.task['diagnosis']['summary']='隔离轮询快照 '+str(body['snapshot'])[:80]+'（合成任务）'
            if 'status' in body:
                self.task['status']=body['status']
                self.task['finished_at']=stamp() if body['status'] in ('succeeded','failed','cancelled','timeout') else None
            self.record('control', mode=self.mode, retry_after=self.retry_after, plain=self.plain, latency_ms=self.latency_ms, status=self.task['status'])
            return {'mode':self.mode,'task_id':self.task['task_id'],'status':self.task['status']}


def handler_for(fixture):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def log_message(self, *args): pass  # No request headers, credentials, or login bodies in logs.
        def send(self, status, body, mime='application/json', extra=None):
            raw=json.dumps(body,ensure_ascii=False).encode() if mime=='application/json' else body.encode() if isinstance(body,str) else body
            try:
                self.send_response(status);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(raw)))
                self.send_header('Cache-Control','no-store')
                for k,v in (extra or {}).items():self.send_header(k,v)
                self.end_headers();self.wfile.write(raw)
            except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):
                fixture.record('client_aborted', path=urlsplit(self.path).path)
        def envelope(self, data=None, status=200, message='ok', extra=None):
            self.send(status,{'code':0 if status==200 else status*100,'message':message,'data':data},extra=extra)
        def do_POST(self):
            path=urlsplit(self.path).path
            if int(self.headers.get('Content-Length','0'))>4096:return self.envelope(status=413,message='fixture_body_limit')
            try:body=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))) or '{}')
            except ValueError:return self.envelope(status=400,message='invalid_json')
            if path=='/__ma_c10/probe':
                allowed={'page_id','kind','is_trusted','visibility_state','hidden','page_time_ms','client_time','status','snapshot','error','user_agent'}
                if set(body)-allowed or body.get('visibility_state') not in ('visible','hidden'):return self.envelope(status=400,message='invalid_observation')
                fixture.record('browser_observation', observation=body)
                page_id=body.get('page_id','')
                if len(page_id)!=36 or any(c not in '0123456789abcdef-' for c in page_id):return self.envelope(status=400)
                return self.envelope(extra={'Set-Cookie':'ma_c10_page_id='+page_id+'; Path=/; SameSite=Strict; HttpOnly'})
            if path=='/__ma_c10/control':
                if not hmac.compare_digest(self.headers.get('X-Test-Control',''),fixture.key):return self.envelope(status=403,message='invalid_control')
                try:return self.envelope(fixture.change(body))
                except ValueError:return self.envelope(status=400,message='invalid_control')
            if path=='/api/v1/auth/login':
                if body != {'username':'ma_c10','password':'fixture-only'}:return self.envelope(status=401,message='use_synthetic_fixture_login')
                return self.envelope({'token':'ma-c10-fixture-only','token_type':'Bearer','expires_in':3600,'user':{'id':991001,'username':'ma_c10','real_name':'MA-C10隔离验收','role':'operator'}})
            if path=='/api/v1/agent/tasks/'+fixture.task['task_id']+'/cancel':
                if self.headers.get('Authorization')!='Bearer ma-c10-fixture-only':return self.envelope(status=401,message='fixture_login_required')
                fixture.change({'mode':'normal','scenario':'cancel','status':'cancelled'});return self.envelope(copy.deepcopy(fixture.task))
            return self.envelope(status=405,message='fixture_no_business_writes')
        def do_GET(self):
            path=unquote(urlsplit(self.path).path)
            if path=='/__ma_c10/probe.js':
                return self.send(200,(Path(__file__).parent/'ma_c10_probe.js').read_bytes(),'text/javascript')
            if path=='/health':return self.envelope({'fixture':'MA-C10','external_calls':0,'task_id':fixture.task['task_id']})
            if path.startswith('/api/'):
                if self.headers.get('Authorization')!='Bearer ma-c10-fixture-only':return self.envelope(status=401,message='fixture_login_required')
                if path=='/api/v1/agent/tasks/'+fixture.task['task_id']:
                    with fixture.lock:
                        mode, retry, plain, latency = fixture.mode, fixture.retry_after, fixture.plain, fixture.latency_ms
                        task=copy.deepcopy(fixture.task)
                    cookie=SimpleCookie(self.headers.get('Cookie',''));page_id=cookie['ma_c10_page_id'].value if 'ma_c10_page_id' in cookie else 'unobserved'
                    fixture.record('detail_request', mode=mode, retry_after=retry, snapshot=task['diagnosis']['summary'],status=task['status'],page_id=page_id)
                    if latency:time.sleep(latency/1000)
                    if mode=='reset':
                        fixture.record('socket_reset', mode=mode)
                        self.connection.setsockopt(socket.SOL_SOCKET,socket.SO_LINGER,struct.pack('hh',1,0))
                        self.close_connection=True;self.connection.close();return
                    if mode=='rate':
                        fixture.record('detail_response',response_status=429,retry_after=retry)
                        if plain:return self.send(429,'fixture rate limit','text/plain',{'Retry-After':retry})
                        return self.envelope(status=429,message='fixture rate limit',extra={'Retry-After':retry})
                    fixture.record('detail_response',response_status=200,status=task['status'])
                    return self.envelope(task)
                if path=='/api/v1/agent/tasks':return self.envelope({'list':[copy.deepcopy(fixture.task)],'offset':0,'page_size':20})
                if path=='/api/v1/agent/sessions':return self.envelope({'list':[],'offset':0,'page_size':20})
                if path=='/api/v1/agent/interpretations':return self.envelope({'list':[],'total':0,'page':1,'page_size':100})
                return self.envelope(status=404,message='fixture_only_no_forwarding')
            file=(fixture.static_root/path.lstrip('/')).resolve()
            if not file.is_relative_to(fixture.static_root):return self.envelope(status=404)
            if not file.is_file():
                if Path(path).suffix:return self.envelope(status=404)
                file=fixture.static_root/'index.html'
            raw=file.read_bytes()
            if file.name=='index.html':raw=raw.replace(b'</body>',b'<script defer src="/__ma_c10/probe.js"></script></body>')
            return self.send(200,raw,'text/javascript' if file.suffix=='.js' else mimetypes.guess_type(file.name)[0] or 'application/octet-stream')
    return Handler


def main():
    parser=argparse.ArgumentParser();sub=parser.add_subparsers(dest='command',required=True)
    serve=sub.add_parser('serve');serve.add_argument('--run',type=Path,required=True);serve.add_argument('--static-root',type=Path,required=True);serve.add_argument('--port',type=int,default=15174)
    control=sub.add_parser('control');control.add_argument('--run',type=Path,required=True);control.add_argument('--mode',choices=['normal','rate','reset'],default='normal');control.add_argument('--scenario',required=True);control.add_argument('--retry-after',default='8');control.add_argument('--plain',action='store_true');control.add_argument('--snapshot');control.add_argument('--status',choices=['queued','planning','executing','succeeded','failed','cancelled','timeout']);control.add_argument('--latency-ms',type=int,default=0)
    args=parser.parse_args();root=Path(__file__).resolve().parents[3];run=args.run.resolve()
    if not run.is_relative_to(root/'artifacts/stage9-ops-agent'):raise SystemExit('dedicated_artifact_batch_required')
    if args.command=='control':
        ledger=json.loads((run/'gateway.json').read_text(encoding='utf8'));port=ledger['port']
        if port not in (15174,15175):raise SystemExit('invalid_fixture_port')
        body={'mode':args.mode,'scenario':args.scenario,'retry_after':args.retry_after,'plain':args.plain,'latency_ms':args.latency_ms}
        for key in ('snapshot','status'):
            if getattr(args,key) is not None:body[key]=getattr(args,key)
        key=(run/'private/c10-control-key').read_text(encoding='utf8').strip()
        req=Request('http://127.0.0.1:'+str(port)+'/__ma_c10/control',data=json.dumps(body).encode(),headers={'X-Test-Control':key,'Content-Type':'application/json'})
        with build_opener(ProxyHandler({})).open(req,timeout=5) as reply:print(reply.read().decode())
        return
    static=args.static_root.resolve()
    if not static.is_relative_to(run) or args.port not in (15174,15175) or not (static/'index.html').is_file():raise SystemExit('isolated_frontend_build_required')
    run.mkdir(parents=True,exist_ok=True);fixture=Fixture(run,static)
    assets=[{'path':p.relative_to(static).as_posix(),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in sorted(static.rglob('*')) if p.is_file()]
    probe=Path(__file__).parent/'ma_c10_probe.js'
    (run/'gateway.json').write_text(json.dumps({'port':args.port,'task_id':fixture.task['task_id'],'external_calls':0,'static_root':str(static.relative_to(root)),'assets':assets,'probe_sha256':hashlib.sha256(probe.read_bytes()).hexdigest()},ensure_ascii=False,indent=2),encoding='utf8')
    ThreadingHTTPServer(('127.0.0.1',args.port),handler_for(fixture)).serve_forever()


if __name__=='__main__':main()
