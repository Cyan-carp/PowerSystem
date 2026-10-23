"""Install an event-only secret over SSH; getpass runs on the remote TTY.

No secret is accepted as a CLI argument, written locally, or printed.
"""
import argparse
import shlex
import subprocess

REMOTE = r'''
import getpass,json,os,re,stat
from pathlib import Path
directory=Path('/etc/powersystem')
directory.mkdir(mode=0o700,parents=True,exist_ok=True)
target=directory/'interpret-deepseek-key'
if target.exists() or target.is_symlink():
    raise SystemExit('target_exists_no_overwrite')
value=getpass.getpass('Official DeepSeek key (hidden): ').strip()
if not re.fullmatch(r'sk-[A-Za-z0-9_-]{20,160}',value):
    raise SystemExit('invalid_key_format')
fd=os.open(str(target),os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o400)
with os.fdopen(fd,'w') as stream:
    stream.write(value+'\n')
    stream.flush()
    os.fsync(stream.fileno())
os.chown(target,10001,10001)
print(json.dumps({'installed':True,'path':str(target),'mode':'0400','owner':10001}))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True)
    args = parser.parse_args()
    command = ['ssh', '-tt', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
               '-o', 'ConnectTimeout=10', args.host, 'python3 -c ' + shlex.quote(REMOTE)]
    raise SystemExit(subprocess.call(command))


if __name__ == '__main__':
    main()
