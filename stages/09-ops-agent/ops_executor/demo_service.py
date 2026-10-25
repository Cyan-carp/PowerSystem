"""Noncritical loopback-only S07 acceptance service. No business/device access."""
import json,os,time
from pathlib import Path
from http.server import BaseHTTPRequestHandler,HTTPServer

def main():
    root=Path(os.environ['S07_SERVICE_ROOT']).resolve()
    root.mkdir(parents=True,exist_ok=True)
    instance=os.environ['S07_SERVICE_INSTANCE'];generation=int(os.environ['S07_SERVICE_GENERATION'])
    def event(name):
        with (root/'events.jsonl').open('a',encoding='utf8') as f:f.write(json.dumps(dict(event=name,level='info',ts=time.time(),count=generation))+'\n')
    event('service_started')
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            if self.path!='/health':self.send_error(404);return
            healthy=not (root/'unhealthy').exists()
            value=dict(instance=instance,pid=os.getpid(),generation=generation,status='healthy' if healthy else 'unhealthy')
            data=json.dumps(value).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    HTTPServer(('127.0.0.1',28976),Handler).serve_forever()

if __name__=='__main__':main()
