"""Root-owned forced entry invoked as the locked-password SSH account."""
import os
import sys

if len(sys.argv)!=1 or os.environ.get('SSH_ORIGINAL_COMMAND'):
    sys.stderr.write('guard_blocked\n')
    raise SystemExit(1)
os.execve('/usr/bin/sudo',['sudo','-n','/usr/local/libexec/powersystem-ops-read'],{'PATH':'/usr/bin:/bin'})
