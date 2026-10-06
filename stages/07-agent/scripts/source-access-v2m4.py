"""Technical source reachability only; never substitute professional review."""
import argparse
import concurrent.futures
import hashlib
import ipaddress
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def check(url):
    original=url
    result={'url_sha256':hashlib.sha256(url.encode()).hexdigest(),'status':'unverified'}
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    for _ in range(4):
        parts=urllib.parse.urlsplit(url)
        if parts.scheme!='https' or not parts.hostname or parts.username or parts.password or parts.port not in (None,443):
            return result|{'status':'unsafe_destination_rejected'}
        try:
            addresses=socket.getaddrinfo(parts.hostname,443,type=socket.SOCK_STREAM)
            if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
                return result|{'status':'non_public_dns_unverified'}
            request=urllib.request.Request(url,headers={'User-Agent':'PowerSystem-V2M4-source-reachability/1.0'})
            try:
                response=opener.open(request,timeout=8)
            except urllib.error.HTTPError as error:
                response=error
            with response:
                code=response.code
                if code in (301,302,303,307,308):
                    url=urllib.parse.urljoin(url,response.headers.get('Location',''))
                    continue
                return result|{'http_status':code,'status':'reachable' if 200<=code<300 else 'http_rejected',
                               'redirected':url!=original,'content_professionally_reviewed':False}
        except (OSError,ValueError,urllib.error.URLError):
            return result|{'status':'network_or_tls_unavailable'}
    return result|{'status':'redirect_limit'}


def main(args):
    if args.output.exists():raise FileExistsError('fresh source audit output required')
    claims=json.loads(args.review.read_text(encoding='utf-8'))
    urls=sorted({source['url'] for row in claims for source in row['sources']})
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:rows=list(pool.map(check,urls))
    counts={status:sum(row['status']==status for row in rows) for status in sorted({row['status'] for row in rows})}
    report={'completed':True,'checked_at':datetime.now(timezone.utc).isoformat(),'claim_count':len(claims),
            'unique_sources':len(urls),'classifications':counts,'sources':rows,'professional_review':'pending',
            'boundary':'HTTP reachability only; no technical claims or real equipment applicability accepted',
            'review_sha256':hashlib.sha256(args.review.read_bytes()).hexdigest()}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:report[k] for k in ('completed','claim_count','unique_sources','classifications','professional_review')}))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--review',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    main(parser.parse_args())
