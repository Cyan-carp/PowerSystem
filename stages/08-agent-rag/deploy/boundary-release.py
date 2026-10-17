"""R730XD-only incremental release with persistent data preserved on rollback."""
import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import tarfile

ROOT = Path('/opt/powersystem')
SERVICES = ('api', 'frontend', 'agent-rag')
FILES = ('stages/04-frontend/deploy/compose.yaml', 'stages/07-agent/deploy/agent.compose.yaml',
         'stages/07-agent/deploy/search.compose.yaml', 'stages/08-agent-rag/deploy/production.compose.yaml')


def inspect(service):
    return json.loads(subprocess.check_output(['docker','inspect','powersystem-stage4-'+service+'-1']))[0]


def baseline_sources(root):
    """Preflight the isolated notes layout without moving server runtime data."""
    saved = set()
    version_notes = 'notes/01-项目调研规划与开发建设记录/第二版-智能体升级'
    for folder in ('stages/02-backend', 'stages/04-frontend', 'stages/08-agent-rag', version_notes):
        for path in (root/folder).rglob('*'):
            if path.is_file() and not any(part in ('node_modules', 'dist', '__pycache__', '.venv', '.git', 'private', 'runtime', 'artifacts', '.env') for part in path.relative_to(root).parts):
                saved.add(path.relative_to(root).as_posix())
    for name in ('README.md', '项目工程规范.md', '项目边界与协作红线.md', '项目验收与收尾清单.md', '运行产物说明.md'):
        path = root/name if name == 'README.md' else root/'notes/02-工程规范边界与验收治理'/name
        if not path.is_file():
            raise FileNotFoundError('missing baseline document: '+name)
        saved.add(path.relative_to(root).as_posix())
    return sorted(saved)


def run(args):
    assert os.uname().nodename == 'R730XD', 'release must run on the specified business host'
    r=args.release.resolve(); assert r.is_relative_to(ROOT/'runtime')
    assert re.fullmatch(r'[a-z0-9-]*', args.revision)
    assert re.fullmatch(r'[a-z0-9][a-z0-9-]{0,79}', r.name), 'release directory must provide a unique image namespace'
    tag=r.name+args.revision
    baseline_tag='baseline-'+r.name
    source_paths = baseline_sources(ROOT) if args.mode == 'prepare' else None
    r.mkdir(parents=True,exist_ok=True);r.chmod(0o700)
    if args.mode == 'prepare':
        assert not (r/'baseline.json').exists(), 'baseline already exists; never overwrite'
        rows={s:inspect(s) for s in SERVICES}
        (r/'baseline.json').write_text(json.dumps({s:{'image':v['Image'],'started_at':v['State']['StartedAt']} for s,v in rows.items()},indent=2))
        (r/'private').mkdir(mode=0o700)
        current_env=Path(rows['agent-rag']['Config']['Labels']['com.docker.compose.project.environment_file'])
        assert current_env.resolve().is_relative_to(ROOT/'runtime') and current_env.is_file()
        shutil.copyfile(current_env,r/'private/compose.env')
        (r/'private/compose.env').chmod(0o600)
        with tarfile.open(r/'source-baseline.tar.gz','w:gz') as out:
            saved=set(source_paths)
            for name in source_paths:
                out.add(ROOT/name,arcname=name)
            # Include every existing candidate document/configuration in the source rollback.
            with tarfile.open(r/'candidate.tar.gz') as candidate:
                for m in candidate.getmembers():
                    if not m.isfile() or m.name=='api-server' or m.name.startswith('dist/') or m.name in saved: continue
                    target=(ROOT/m.name).resolve();assert target.is_relative_to(ROOT)
                    if target.is_file():
                        out.add(target,arcname=m.name)
                        saved.add(m.name)
        for s,v in rows.items():
            subprocess.run(['docker','tag',v['Image'],'powersystem-bnd-'+s+':'+baseline_tag],check=True)
        (r/'baseline.yaml').write_text('services:\n'+''.join('  '+s+':\n    image: powersystem-bnd-'+s+':'+baseline_tag+'\n' for s in SERVICES))
        with (r/'private/agent-before.dump').open('wb') as stream:
            subprocess.run(['docker','exec','powersystem-stage4-postgres-1','pg_dump','-U','powersystem','-d','powersystem_agent_rag','-Fc'],stdout=stream,check=True)
        (r/'private/agent-before.dump').chmod(0o600)
        print('baseline source, images, configuration and Agent database saved')
        return
    if args.mode == 'build':
        with tarfile.open(r/'candidate.tar.gz') as archive:
            for m in archive.getmembers():
                target=(r/'candidate'/m.name).resolve();assert target.is_relative_to(r/'candidate') and m.isfile()
                target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(archive.extractfile(m).read())
        c=r/'candidate'
        # Older incremental packages may omit unchanged importer/knowledge files.
        # Copy reviewed updates when present; otherwise keep the baseline image.
        rag_updates=''.join(f'COPY stages/08-agent-rag/{name} /app/{name}\n'
                            for name in ('scripts', 'knowledge') if (c/'stages/08-agent-rag'/name).is_dir())
        for s,text in {
            'api':f'FROM powersystem-bnd-api:{baseline_tag}\nCOPY --chmod=755 api-server /app/server\n',
            'frontend':f'FROM powersystem-bnd-frontend:{baseline_tag}\nCOPY dist /usr/share/nginx/html\n',
            'agent-rag':f'FROM powersystem-bnd-agent-rag:{baseline_tag}\nUSER root\nCOPY stages/08-agent-rag/rag_agent /app/rag_agent\nCOPY stages/08-agent-rag/deploy /app/deploy\n{rag_updates}USER 10002:10002\n',
        }.items():
            (c/('Dockerfile.'+s)).write_text(text)
            subprocess.run(['docker','build','--network=none','--pull=false','-f',str(c/('Dockerfile.'+s)),'-t','powersystem-bnd-'+s+':'+tag,str(c)],check=True)
        (r/'candidate.yaml').write_text('services:\n'+''.join('  '+s+':\n    image: powersystem-bnd-'+s+':'+tag+'\n' for s in SERVICES))
        return
    if args.mode not in ('activate','rollback'): raise ValueError('unknown mode')
    archivepath=r/('source-baseline.tar.gz' if args.mode=='rollback' else 'candidate.tar.gz')
    with tarfile.open(archivepath) as archive:
        members=[m for m in archive.getmembers() if m.isfile() and (args.mode=='rollback' or m.name not in ('api-server',) and not m.name.startswith('dist/'))]
        baseline={m.name for m in members}
        for m in members:
            target=(ROOT/m.name).resolve();assert target.is_relative_to(ROOT)
            target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(archive.extractfile(m).read())
            target.chmod(0o750 if target.suffix=='.sh' else m.mode & 0o755)
        if args.mode=='rollback':
            with tarfile.open(r/'candidate.tar.gz') as candidate:
                for m in candidate.getmembers():
                    if not m.isfile() or m.name in baseline or not (m.name.startswith(('stages/02-backend/','stages/04-frontend/','stages/08-agent-rag/')) or m.name.endswith('.md') or m.name=='.env.example'): continue
                    target=(ROOT/m.name).resolve();assert target.is_relative_to(ROOT)
                    if target.is_file() and hashlib.sha256(target.read_bytes()).digest()==hashlib.sha256(candidate.extractfile(m).read()).digest(): target.unlink()
    cmd=['docker','compose','--env-file',str(r/'private/compose.env')]
    for f in FILES:cmd+=['-f',str(ROOT/f)]
    cmd+=['-f',str(r/('baseline.yaml' if args.mode=='rollback' else 'candidate.yaml')),'--profile','agent','up','-d','--no-deps','--no-build','--pull','never','--force-recreate','--wait',*SERVICES]
    try: subprocess.run(cmd,cwd=ROOT,check=True)
    except subprocess.CalledProcessError:
        if args.mode=='activate':
            subprocess.run(['python3',str(__file__),'rollback','--release',str(r)],check=True)
        raise
    print(args.mode+' complete; database and vector volumes retained')


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['prepare','build','activate','rollback']);p.add_argument('--release',required=True,type=Path);p.add_argument('--revision',default='')
    run(p.parse_args())
