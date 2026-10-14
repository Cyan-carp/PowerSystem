"""Switch only API/frontend; release assets and data are retained for recovery."""
import argparse,hashlib,json,pathlib,subprocess,tarfile

parser=argparse.ArgumentParser();parser.add_argument('mode',choices=['activate','rollback']);parser.add_argument('--release',type=pathlib.Path,required=True)
args=parser.parse_args();r=args.release.resolve();root=pathlib.Path('/opt/powersystem')
assert r.is_relative_to(root/'runtime')
if args.mode=='rollback':
    with tarfile.open(r/'source-baseline.tar.gz') as archive:
        baseline_paths={member.name for member in archive.getmembers()}
        for member in archive.getmembers():
            target=(root/member.name).resolve();assert target.is_relative_to(root) and member.isfile()
            target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(archive.extractfile(member).read());target.chmod(0o600 if member.name=='.env' else member.mode & 0o755)
    # Remove only release-added backend/frontend files, so the old source can build.
    for row in json.loads((r/'candidate-manifest.json').read_text()):
        if row['path'] not in baseline_paths and row['path'].startswith(('stages/02-backend/','stages/04-frontend/')):
            target=(root/row['path']).resolve();assert target.is_relative_to(root)
            if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest()==row['sha256']:target.unlink()
    paths=json.loads((r/'private/baseline-compose.json').read_text())+[str(r/'baseline-images.yaml')]
    env=root/'.env'
else:
    rows={v['path']:v for v in json.loads((r/'candidate-manifest.json').read_text())}
    with tarfile.open(r/'candidate.tar.gz') as archive:
        for member in archive.getmembers():
            target=(root/member.name).resolve();assert target.is_relative_to(root) and member.isfile()
            raw=archive.extractfile(member).read();assert hashlib.sha256(raw).hexdigest()==rows[member.name]['sha256']
            mode=(target.stat().st_mode & 0o755) if target.exists() else (0o750 if target.suffix=='.sh' else 0o644)
            target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(raw);target.chmod(mode)
    paths=[str(root/p) for p in ('stages/04-frontend/deploy/compose.yaml','stages/07-agent/deploy/agent.compose.yaml','stages/07-agent/deploy/search.compose.yaml','stages/08-agent-rag/deploy/production.compose.yaml')]+[str(r/'current.yaml')]
    env=r/'private/compose.env'
command=['docker','compose','--env-file',str(env)]
for path in paths:command+=['-f',path]
command+=['--profile','agent','up','-d','--no-deps','--no-build','--pull','never','--force-recreate','--wait','api','frontend']
try:
    subprocess.run(command,cwd=root,check=True)
except subprocess.CalledProcessError:
    if args.mode=='activate':
        # An activation error immediately restores the previous serving images.
        subprocess.run(['python3',str(pathlib.Path(__file__).resolve()),'rollback','--release',str(r)],check=True)
    raise
print(args.mode+' complete')
