"""Cold-copy content and ownership verification. Manifests stay on the servers."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def scan(root):
    root=Path(root)
    entries={}
    paths=[root]
    if root.is_dir() and not root.is_symlink():
        for directory,subdirs,files in os.walk(root,followlinks=False):
            paths.extend(Path(directory)/name for name in subdirs+files)
    total=0
    for path in sorted(set(paths)):
        info=path.lstat();key='.' if path==root else path.relative_to(root).as_posix()
        item={'mode':stat.S_IMODE(info.st_mode),'uid':info.st_uid,'gid':info.st_gid}
        if stat.S_ISREG(info.st_mode):
            h=hashlib.sha256()
            with path.open('rb') as stream:
                for block in iter(lambda:stream.read(8<<20),b''):h.update(block)
            item.update(kind='file',bytes=info.st_size,sha256=h.hexdigest())
            total+=info.st_size
        elif stat.S_ISDIR(info.st_mode):item['kind']='directory'
        elif stat.S_ISLNK(info.st_mode):item.update(kind='symlink',target=os.readlink(path))
        else:raise ValueError('unsupported_cold_file_type')
        xattrs={name:hashlib.sha256(os.getxattr(path,name,follow_symlinks=False)).hexdigest()
                for name in sorted(os.listxattr(path,follow_symlinks=False))}
        if xattrs:item['xattrs']=xattrs
        entries[key]=item
    return {'sha256':digest(entries),'entries':entries,'logical_bytes':total,'entry_count':len(entries)}


def snapshot(mapping,prefix=None):
    result={}
    for alias,source in mapping.items():
        path=Path(source)
        if not path.is_absolute() or '..' in path.parts:raise ValueError('invalid_cold_source')
        if not (str(path).startswith('/var/lib/docker/volumes/') or
                str(path).startswith('/opt/powersystem/') or
                str(path).startswith('/etc/powersystem/') or
                str(path).startswith('/etc/powersystem-ops-read/')):
            raise ValueError('cold_source_outside_project')
        actual=Path(prefix)/str(path).lstrip('/') if prefix else path
        result[alias]={'source':source,**scan(actual)}
    return result


def compact(result):
    return {alias:{k:row[k] for k in ('source','sha256','logical_bytes','entry_count')}
            for alias,row in result.items()}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--roots',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--prefix')
    parser.add_argument('--expected')
    args=parser.parse_args()
    roots=json.loads(Path(args.roots).read_text())
    assert len([k for k in roots if k.startswith('volume:')])==13
    result=snapshot(roots,args.prefix)
    if args.expected:
        expected=json.loads(Path(args.expected).read_text())
        assert set(result)==set(expected),'cold_root_set_changed'
        mismatches=[alias for alias in result if result[alias]['sha256']!=expected[alias]['sha256']]
        if mismatches:raise ValueError('cold_root_mismatch:'+','.join(mismatches))
    target=Path(args.output);target.write_text(json.dumps(result,ensure_ascii=False,indent=2));target.chmod(0o600)
    print(json.dumps({'verified':True,'roots':compact(result)},ensure_ascii=False))


if __name__=='__main__':main()
