"""Sync reviewed test/document changes while proving running assets unchanged."""
import argparse
import hashlib
import json
import tarfile
from pathlib import Path, PurePosixPath


def administrative(path):
    name=path.as_posix()
    return (path.suffix=='.md' and (len(path.parts)==1 or name.startswith(('第一版-平台主体/','第二版-智能体升级/','stages/07-agent/')))
            or name.startswith(('stages/07-agent/scripts/','stages/07-agent/tests/','stages/02-backend/scripts/'))
            or name.startswith('stages/02-backend/internal/api/') and path.name.endswith('_test.go')
            or name in ('stages/07-agent/deploy/events-v2m4.py','stages/07-agent/deploy/sync-v2m4-source.py'))


def main(args):
    root=args.root.resolve(strict=True)
    if args.output.exists():raise FileExistsError('fresh source proof output required')
    manifest=json.loads(args.manifest.read_text(encoding='utf-8'))
    assert hashlib.sha256(args.archive.read_bytes()).hexdigest()==manifest['archive_sha256']
    baseline={row['path']:row['sha256'] for row in json.loads(args.runtime_manifest.read_text(encoding='utf-8'))}
    approved={row['path']:row['sha256'] for row in manifest['files']}
    changes=[];members=[]
    with tarfile.open(args.archive) as archive:
        entries=archive.getmembers()
        assert len(entries)==len(approved) and len({item.name for item in entries})==len(entries)
        for entry in entries:
            path=PurePosixPath(entry.name);target=root/entry.name
            assert entry.isfile() and not path.is_absolute() and '..' not in path.parts
            assert not any(part in ('.git','.env','runtime','artifacts','.obsidian','docx工作区') for part in path.parts)
            assert not target.is_symlink() and target.resolve().is_relative_to(root)
            raw=archive.extractfile(entry).read();digest=hashlib.sha256(raw).hexdigest()
            assert digest==approved[entry.name]
            runtime_changed=baseline.get(entry.name)!=digest
            assert not runtime_changed or administrative(path), 'running asset changed: '+entry.name
            if not administrative(path):
                assert target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest()==digest, 'server runtime drift: '+entry.name
            if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest()!=digest:
                changes.append(entry.name)
            members.append((target,raw))
        # Preflight every member before making any changes. No services restarted.
        for target,raw in members:
            if target.is_file() and target.read_bytes()==raw:continue
            target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(raw);target.chmod(0o644)
    assert all(hashlib.sha256((root/path).read_bytes()).hexdigest()==digest for path,digest in approved.items())
    report={'completed':True,'passed':True,'source_commit':manifest['commit'],'source_base':manifest['base'],
            'archive_sha256':manifest['archive_sha256'],'files':manifest['files'],'updated_administrative_files':changes,
            'running_assets_changed':False,'services_restarted':False}
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'passed':True,'source_commit':manifest['commit'],'files':len(approved),'administrative_changes':len(changes),'running_assets_changed':False}))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True);parser.add_argument('--archive',type=Path,required=True)
    parser.add_argument('--manifest',type=Path,required=True);parser.add_argument('--runtime-manifest',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    main(parser.parse_args())
