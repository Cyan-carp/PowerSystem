"""Public-note links and credential scan; report locations, never secret text."""
import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

ROOT=Path(__file__).resolve().parents[3]


def main(args):
    tracked=subprocess.check_output(['git','ls-files','-z'],cwd=ROOT).decode('utf-8').split('\0')
    added=subprocess.check_output(['git','ls-files','--others','--exclude-standard','-z'],cwd=ROOT).decode('utf-8').split('\0')
    paths=[ROOT/p for p in sorted(set(tracked+added)) if p and not p.startswith(('.obsidian/','docx工作区/'))]
    paths += list((ROOT/'stages/04-frontend/dist').rglob('*'))
    secrets=set()
    for config in args.config:
        for line in config.read_text(encoding='utf-8-sig').splitlines():
            if '=' not in line or line.startswith('#'):continue
            key,value=line.split('=',1)
            if key.endswith('_FILE') and Path(value).is_file():
                value=Path(value).read_text(encoding='utf-8-sig').strip()
            elif not re.search('SECRET|PASSWORD|TOKEN|API_KEY|SEARCH_KEY',key):continue
            if len(value)>=12:secrets.add(value)
    patterns=[re.compile(x) for x in (r'-----BEGIN (?:OPENSSH |RSA |EC )?PRIVATE KEY-----',r'gh[pousr]_[A-Za-z0-9]{30,}',r'github_pat_[A-Za-z0-9_]{40,}',r'sk-[A-Za-z0-9]{24,}')]
    hits=[];links=[];hashes={}
    notes=[p for p in paths if p.suffix=='.md']
    known={p.relative_to(ROOT).as_posix().removesuffix('.md') for p in notes}
    names={p.stem for p in notes}
    for path in paths:
        if not path.is_file():continue
        raw=path.read_bytes();label=path.relative_to(ROOT).as_posix()
        hashes[label]=hashlib.sha256(raw).hexdigest()
        try:content=raw.decode('utf-8-sig')
        except UnicodeDecodeError:continue
        for number,line in enumerate(content.splitlines(),1):
            count=sum(line.count(value) for value in secrets)+sum(len(pattern.findall(line)) for pattern in patterns)
            if count:hits.append({'path':label,'line':number,'count':count})
        if path.suffix=='.md':
            for target in re.findall(r'\[\[([^\]]+)\]\]',content):
                target=target.split('|',1)[0].split('#',1)[0].removesuffix('.md')
                if not target or '<' in target:continue
                exists=(ROOT/target).is_file() or (ROOT/(target+'.md')).is_file() or (path.parent/target).is_file() or (path.parent/(target+'.md')).is_file() or target in known or ('/' not in target and target in names)
                if not exists:links.append({'path':label,'target':target})
    report={'completed':True,'passed':not hits,'credential_hits':hits,'credential_hit_count':sum(x['count'] for x in hits),'files_scanned':len(hashes),'broken_links':links,'sha256':hashes}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:report[k] for k in ('passed','files_scanned','credential_hit_count','broken_links')},ensure_ascii=False))
    return report['passed']


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--config',type=Path,nargs='+',required=True);p.add_argument('--output',type=Path,required=True)
    raise SystemExit(0 if main(p.parse_args()) else 1)
