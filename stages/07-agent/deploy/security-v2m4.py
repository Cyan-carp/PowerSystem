"""External anonymous release checks; no configuration or secret output."""
import argparse
import json
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def main(args):
    args.output.mkdir(parents=True,exist_ok=True)
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    rows=[]
    def request(path,body=None):
        data=json.dumps(body).encode() if body is not None else None
        req=urllib.request.Request(args.url+path,data=data,headers={'Content-Type':'application/json'})
        try:
            with opener.open(req,timeout=10) as result:return result.status
        except urllib.error.HTTPError as error:return error.code
    for path,body,expected in [('/api/v1/ping',None,200),('/internal/agent/health',None,403),
        ('/internal/agent/model-status',None,403),('/api/v1/agent/interpretations',None,401),
        ('/api/v1/agent/chat',{'message':'查询'},401),('/api/v1/auth/register',{'username':'v2m4-forbidden'},403)]:
        code=request(path,body);rows.append({'case':path,'code':code,'passed':code==expected})
    attempts=[]
    for i in range(16):
        code=request('/api/v1/auth/login',{'username':'v2m4-not-an-account','password':'invalid-test-password'})
        attempts.append(code)
        if code==429:break
    rows.append({'case':'login rate limit','codes':attempts,'passed':429 in attempts})
    parsed=urllib.parse.urlsplit(args.url)
    with socket.create_connection((parsed.hostname,parsed.port or 443),timeout=10) as connection:
        with ssl.create_default_context().wrap_socket(connection,server_hostname=parsed.hostname) as secure:
            cert=secure.getpeercert()
            rows.append({'case':'trusted TLS','passed':True,'protocol':secure.version(),'not_after':cert['notAfter']})
    ports=[]
    for port in (3667,8092,8090,5432,6379,6041,1883,13000,19090,2375,2376):
        try:
            with socket.create_connection((parsed.hostname,port),timeout=2): reachable=True
        except OSError:reachable=False
        ports.append({'port':port,'tcp_reachable':reachable,
            'ownership':'shared-mapping-requires-host-inventory' if port==19090 else 'project-sensitive-candidate'})
    # Shared :19090 must be classified against the host FRP inventory.
    rows.append({'case':'project sensitive direct ports','passed':all(not row['tcp_reachable'] for row in ports if row['port']!=19090)})
    report={'completed':True,'passed':all(row['passed'] for row in rows),'cases':rows,'ports':ports,'viewpoint':args.viewpoint}
    (args.output/'security.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps({'passed':report['passed'],'checks':len(rows),'reachable_ports':[x['port'] for x in ports if x['tcp_reachable']]}))
    return report['passed']


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--url',required=True);parser.add_argument('--output',type=Path,required=True);parser.add_argument('--viewpoint',default='external-client')
    args=parser.parse_args();args.url=args.url.rstrip('/')
    raise SystemExit(0 if main(args) else 1)
