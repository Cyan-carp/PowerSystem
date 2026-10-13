"""Close this local acceptance window without deleting audit, containers or approvals."""
import argparse
import json
from pathlib import Path


def main(args):
    root=Path(__file__).resolve().parents[3];run=(root/args.run).resolve()
    if not run.is_relative_to(root/'artifacts/stage9-ops-agent') or not (run/'remote/drill.json').is_file():raise ValueError('owned_m3_batch_required')
    config=run/'private/local.env';text=config.read_text(encoding='utf-8-sig')
    if 'POSTGRES_DB=ma_m3_business_test' not in text:raise ValueError('m3_private_config_required')
    (run/'private/broker/write-window.json').write_text(json.dumps({'write_enabled':False,'window_until':0}),encoding='utf-8')
    config.write_text(text.replace('OPS_WRITE_ENABLED=true','OPS_WRITE_ENABLED=false'),encoding='utf-8')
    print('Local broker write window closed; restart only this run\'s API to load OPS_WRITE_ENABLED=false. Evidence retained.')


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--run',required=True);main(p.parse_args())
