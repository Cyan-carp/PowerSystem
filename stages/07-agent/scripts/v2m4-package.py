"""Build a fixed-commit, public-file incremental deployment package."""
import argparse
import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[3]


def allowed_source(path):
    if path.is_absolute() or any(part in ('..', '.env', '.git', '.venv', 'private', 'runtime', 'artifacts', 'node_modules', '__pycache__') for part in path.parts):
        return False
    name=path.as_posix()
    return (name.startswith(('stages/07-agent/', 'stages/02-backend/internal/api/', 'stages/02-backend/scripts/',
                             '第二版-智能体升级/', '第一版-平台主体/', 'notes/01-项目调研规划与开发建设记录/第二版-智能体升级/', 'notes/01-项目调研规划与开发建设记录/第一版-平台主体/'))
            or path.suffix=='.md' and (len(path.parts)==1 or name.startswith(('notes/02-工程规范边界与验收治理/', 'notes/03-平台使用运维与二次开发手册/', 'notes/04-系统部署发布备份与变更记录/'))))


def main(args):
    def git(*values):return subprocess.check_output(['git',*values],cwd=ROOT)
    commit=git('rev-parse',args.commit+'^{commit}').decode().strip()
    base=git('rev-parse',args.base+'^{commit}').decode().strip()
    names=git('diff','--name-only','-z',base,commit).decode('utf-8').split('\0')
    args.output.mkdir(parents=True,exist_ok=True)
    archive=args.output/'candidate.tar.gz'
    if archive.exists():raise FileExistsError('candidate package already exists')
    rows=[]
    with tarfile.open(archive,'w:gz') as package:
        for name in sorted(x for x in names if x):
            path=Path(name)
            if name.startswith(('.obsidian/','docx工作区/','artifacts/')) or '.env'==path.name or path.is_absolute() or '..' in path.parts:raise ValueError('unapproved package path')
            if not allowed_source(path):raise ValueError('unsupported incremental source')
            raw=git('show',commit+':'+name)
            member=tarfile.TarInfo(name);member.size=len(raw);member.mode=0o644;member.mtime=0
            package.addfile(member,io.BytesIO(raw))
            rows.append({'path':name,'sha256':hashlib.sha256(raw).hexdigest(),'size':len(raw)})
    report={'commit':commit,'base':base,'archive_sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),'files':rows}
    (args.output/'candidate.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'commit':commit,'files':len(rows),'archive_sha256':report['archive_sha256']}))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--commit',required=True);p.add_argument('--base',required=True);p.add_argument('--output',type=Path,required=True);main(p.parse_args())
