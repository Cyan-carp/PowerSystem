"""Ephemeral health service. No storage, credentials or business integration."""
import signal
from http.server import BaseHTTPRequestHandler, HTTPServer


class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path!='/health': self.send_error(404);return
        self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers()
        self.wfile.write(b'{"status":"ok","purpose":"ma-m3-drill"}')
    def log_message(self,*args): pass


if __name__=='__main__':
    signal.signal(signal.SIGTERM,lambda *_:exit(0))
    HTTPServer(('0.0.0.0',8080),Health).serve_forever()
