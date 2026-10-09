"""Read-only production checks for the three simulated inverter references."""
import argparse
import base64
import hashlib
import hmac
import json
import re
from datetime import datetime
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', type=Path, default=Path('/opt/powersystem'))
    parser.add_argument('--url', default='https://8.138.10.222')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    checks = []

    def check(name, passed):
        checks.append({'name': name, 'passed': bool(passed)})
        (args.output / 'checks.json').write_text(json.dumps(checks, ensure_ascii=False, indent=2), encoding='utf-8')
        if not passed:
            raise AssertionError(name)

    def sql(query):
        result = subprocess.run(['docker', 'exec', 'powersystem-stage4-postgres-1', 'psql', '-U',
                                 'powersystem', '-d', 'powersystem', '-Atqc', query], text=True,
                                capture_output=True, check=True)
        return result.stdout.strip()

    rows = sql("SELECT id||'|'||device_code||'|'||vendor||'|'||is_simulated||'|'||reference_vendor||'|'||reference_model "
               "FROM devices WHERE device_code IN ('INV-1001','INV-1002','INV-1003') AND deleted_at IS NULL "
               "ORDER BY device_code").splitlines()
    devices = [row.split('|') for row in rows]
    check('three simulated identities', len(devices) == 3 and
          [row[1] for row in devices] == ['INV-1001', 'INV-1002', 'INV-1003'] and
          all(row[2:] == ['synthetic', 'true', 'Huawei', 'SUN2000-100KTL-M2'] for row in devices))
    check('other devices not relabeled', sql("SELECT count(*) FROM devices WHERE is_simulated "
          "AND device_code NOT IN ('INV-1001','INV-1002','INV-1003')") == '0')

    private = dict(line.split('=', 1) for line in (args.project / '.env').read_text().splitlines()
                   if '=' in line and not line.startswith('#'))
    user_id = int(sql("SELECT id FROM users WHERE role='operator' ORDER BY id LIMIT 1"))
    encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip('=')
    data = encode({'alg': 'HS256', 'typ': 'JWT'}) + '.' + encode({'sub': str(user_id), 'exp': int(time.time()) + 1800})
    token = data + '.' + base64.urlsafe_b64encode(hmac.new(private['JWT_SECRET'].encode(), data.encode(), hashlib.sha256).digest()).decode().rstrip('=')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def api(path, body=None):
        request = urllib.request.Request(args.url + path, data=json.dumps(body).encode() if body else None,
                                         headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        try:
            with opener.open(request, timeout=65) as result:
                return result.status, json.load(result)['data']
        except urllib.error.HTTPError as error:
            return error.code, {}

    for device_id, code, *_ in devices:
        status, item = api('/api/v1/devices/' + device_id)
        check('device API ' + code, status == 200 and item.get('device_code') == code and
              item.get('vendor') == 'synthetic' and item.get('is_simulated') is True and
              item.get('reference_vendor') == 'Huawei' and item.get('reference_model') == 'SUN2000-100KTL-M2')

    def ask(label, question):
        status, response = api('/api/v1/agent/chat', {'message': question})
        (args.output / (label + '.json')).write_text(json.dumps({
            'http_status': status, 'status': response.get('status'), 'answer_mode': response.get('answer_mode'),
            'knowledge_status': response.get('knowledge_status'),
            'evidence': [{'id': e['id'], 'kind': e['kind'], 'tool': e['tool'], 'status': e['status'],
                          'data_time': e.get('data_time'),
                          'source_kind': e.get('data', {}).get('source_kind') if isinstance(e.get('data'), dict) else None}
                         for e in response.get('evidence', [])],
            'conclusion_evidence_ids': (response.get('conclusion') or {}).get('evidence_ids', []),
            'conclusion_text': (response.get('conclusion') or {}).get('text', ''),
            'source_coverage_percent': response.get('source_coverage_percent')}, ensure_ascii=False, indent=2), encoding='utf-8')
        check(label + ' HTTP', status == 200)
        return response

    manual = ask('manual', '华为 SUN2000-100KTL-M2 的额定有功功率是多少？')
    manual_ids = {e['id'] for e in manual.get('evidence', []) if e.get('status') == 'ok' and
                  isinstance(e.get('data'), dict) and e['data'].get('source_kind') == 'manual_summary'}
    check('manual chapter cited', manual.get('status') == 'answered' and manual.get('answer_mode') == 'grounded' and
          bool(manual_ids.intersection((manual.get('conclusion') or {}).get('evidence_ids', []))))

    boundary = ask('simulation-boundary', '华为手册描述的 AFCI 在模拟器里实现了吗？')
    boundary_ids = {e['id'] for e in boundary.get('evidence', []) if e.get('status') == 'ok' and
                    isinstance(e.get('data'), dict) and e['data'].get('source_kind') == 'manual_summary'}
    check('manual function versus simulation', boundary.get('status') == 'answered' and
          bool(boundary_ids.intersection((boundary.get('conclusion') or {}).get('evidence_ids', []))) and
          bool(re.search(r'未.{0,12}(?:实现|生成|接入)|没有.{0,8}实现',
                         (boundary.get('conclusion') or {}).get('text', ''))))

    business = ask('business', '查询所有设备列表，附证据。')
    business_ids = {e['id'] for e in business.get('evidence', []) if e.get('status') == 'ok' and
                    e.get('kind') == 'business' and e.get('tool') == 'list_devices'}
    check('current business data cited', business.get('status') == 'answered' and
          bool(business_ids.intersection((business.get('conclusion') or {}).get('evidence_ids', []))))

    live = ask('live', 'INV-1001 现在功率是多少？')
    check('live tool attempted', any(e.get('kind') == 'business' and e.get('tool') in
          ('get_telemetry', 'get_dashboard_summary') for e in live.get('evidence', [])))
    check('live answer grounded', live.get('status') == 'answered' and live.get('answer_mode') == 'grounded')
    cited = set((live.get('conclusion') or {}).get('evidence_ids', []))
    check('live data cited', any(e['id'] in cited and e.get('status') == 'ok' and e.get('kind') == 'business'
          and e.get('tool') == 'get_telemetry' for e in live.get('evidence', [])))
    stamp_pattern = r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})'
    claimed_stamps = re.findall(stamp_pattern, (live.get('conclusion') or {}).get('text', ''))
    source_stamps = [value for e in live.get('evidence', []) if e['id'] in cited
                     for value in (e.get('data_time'), e.get('collected_at'),
                                   e.get('data', {}).get('received_at') if isinstance(e.get('data'), dict) else None)
                     if value]
    parse = lambda value: datetime.fromisoformat(value.replace('Z', '+00:00'))
    check('live timestamp matches cited evidence', all(any(abs((parse(claim) - parse(source)).total_seconds()) < 1
          for source in source_stamps) for claim in claimed_stamps))

    print(json.dumps({'passed': len(checks), 'checks': len(checks)}))


if __name__ == '__main__':
    main()
