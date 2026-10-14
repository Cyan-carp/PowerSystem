"""Load private isolated fixture configuration without putting secrets in argv."""
import argparse
import os
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[3]

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--run',required=True)
    parser.add_argument('--layer',choices=('sec','ops','rag'),required=True)
    parser.add_argument('--attempt',required=True)
    args=parser.parse_args()
    run=(ROOT/args.run).resolve()
    if not run.is_relative_to(ROOT/'artifacts/stage10-security-agent'):
        raise ValueError('invalid_sec_run')
    import re
    if not re.fullmatch(r'[a-z0-9-]{1,60}',args.attempt):raise ValueError('invalid_attempt')
    for line in (run/'private'/f'{args.layer}-test.env').read_text(encoding='utf-8').splitlines():
        key,value=line.split('=',1);os.environ[key]=value
    if (run/'private/m4-test.env').exists():os.environ['SEC_M4_TEST_ENV_FILE']=str(run/'private/m4-test.env')
    if (run/'private/m3-test.env').exists():os.environ['SEC_M3_TEST_ENV_FILE']=str(run/'private/m3-test.env')
    if args.layer=='sec':os.environ['SEC_TEST_CONFIG_FILE']=str(run/'private/m1-regression.json')
    for name in ('stages/09-ops-agent','stages/08-agent-rag','stages/10-security-agent'):
        sys.path.insert(0,str(ROOT/name))
    os.chdir(ROOT)
    import pytest
    stage={'ops':'09-ops-agent','rag':'08-agent-rag','sec':'10-security-agent'}[args.layer]
    import contextlib
    output=run/f'{args.layer}-{args.attempt}.log'
    with output.open('x',encoding='utf-8') as log,contextlib.redirect_stdout(log),contextlib.redirect_stderr(log):
        code=pytest.main([f'stages/{stage}/tests','-q','-p','no:cacheprovider',
                         '--basetemp',str(run/f'tmp-{args.layer}-{args.attempt}'),
                         '--junitxml',str(run/f'{args.layer}-{args.attempt}.xml')])
    print(output.read_text(encoding='utf-8'))
    sys.exit(code)
