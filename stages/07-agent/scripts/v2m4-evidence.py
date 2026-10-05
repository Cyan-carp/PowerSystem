"""Hash-bound evidence ledger. Missing, skipped or interrupted gates fail delivery."""
import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

REQUIRED = ('local-suite', 'real-interpretations', 'source-sampling', 'main-chain-isolation',
            'server-events', 'server-chat', 'server-search', 'server-limits', 'browser',
            'public-security', 'secret-scan', 'rollback', 'backup-restore', 'observation', 'documentation')


def evaluate(root, records):
    results = {}
    for row in records:
        paths = row.get('evidence', [])
        intact = bool(paths)
        for item in paths:
            path = (root / item['path']).resolve()
            intact = (intact and path.is_relative_to(root.resolve()) and path.is_file()
                      and hashlib.sha256(path.read_bytes()).hexdigest() == item['sha256'])
        results[row['gate']] = row.get('result') == 'passed' and intact
    return all(results.get(gate, False) for gate in REQUIRED)


def main(args):
    root = args.run.resolve(); root.mkdir(parents=True, exist_ok=True)
    ledger = root / 'delivery-ledger.json'
    records = json.loads(ledger.read_text(encoding='utf-8')) if ledger.exists() else []
    if args.gate:
        if any(row['gate'] == args.gate for row in records): raise ValueError('gate already recorded; use a new full run for failed delivery')
        evidence=[]
        for supplied in args.evidence:
            path=supplied.resolve()
            if not path.is_relative_to(root) or not path.is_file(): raise ValueError('evidence must exist in this run')
            evidence.append({'path':path.relative_to(root).as_posix(),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
        records.append({'gate':args.gate,'task': 'V2-T12' if args.gate in ('server-limits','public-security','secret-scan') else 'V2-T13' if args.gate in ('documentation','backup-restore','rollback','observation') else 'V2-T11',
                        'result':args.result,'recorded_at':datetime.now(timezone.utc).isoformat(),'evidence':evidence,'note':args.note})
        ledger.write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf-8')
    passed=evaluate(root,records)
    summary={'required_gates':list(REQUIRED),'recorded_gates':len(records),'passed':passed,
             'professional_review':'pending','real_device_applicability_verified':False}
    (root/'delivery-summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    print(json.dumps(summary))
    return passed


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--run',type=Path,required=True)
    parser.add_argument('--gate',choices=REQUIRED)
    parser.add_argument('--result',choices=('passed','failed','interrupted','skipped'),default='passed')
    parser.add_argument('--evidence',type=Path,nargs='*',default=[])
    parser.add_argument('--note',default='')
    args=parser.parse_args()
    passed=main(args)
    raise SystemExit(0 if args.gate or passed else 1)
