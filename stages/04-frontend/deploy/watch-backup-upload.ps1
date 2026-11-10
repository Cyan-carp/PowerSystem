param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^powersystem-[0-9]{8}T[0-9]{6}Z$')]
    [string]$RunId,

    [ValidateRange(5, 300)]
    [int]$IntervalSeconds = 10,

    [switch]$Once
)

$ErrorActionPreference = 'Stop'
$probe = @'
import datetime
import json
import os
from pathlib import Path
import sys

run_id = sys.argv[1]
root = Path('/var/backups/powersystem')
archive = root / (run_id + '.tar.gz.gpg')
stage_file = root / (run_id + '.stages.ndjson')
timing_file = root / (run_id + '.timing.json')
stages = []
if stage_file.is_file():
    for line in stage_file.read_text().splitlines():
        try:
            stages.append(json.loads(line))
        except ValueError:
            pass

size = archive.stat().st_size if archive.is_file() else 0
uploaded = {}
for row in stages:
    name = row.get('name')
    if name in ('upload_primary', 'upload_secondary'):
        uploaded[name] = {'done': True, 'exit_code': row.get('exit_code')}

read_positions = []
for proc in Path('/proc').iterdir():
    if not proc.name.isdigit():
        continue
    try:
        args = (proc / 'cmdline').read_bytes().split(b'\0')
        if not args or Path(os.fsdecode(args[0])).name != 'scp':
            continue
        if os.fsencode(str(archive)) not in args:
            continue
        for fd in (proc / 'fd').iterdir():
            if os.readlink(fd) != str(archive):
                continue
            for line in (proc / 'fdinfo' / fd.name).read_text().splitlines():
                if line.startswith('pos:'):
                    read_positions.append(int(line.split(':', 1)[1]))
    except (OSError, ValueError):
        continue

timing = None
if timing_file.is_file():
    data = json.loads(timing_file.read_text())
    timing = {'status': data.get('status'), 'failed_stage': data.get('failed_stage')}

print(json.dumps({
    'time': datetime.datetime.now(datetime.timezone.utc).strftime('%H:%M:%S UTC'),
    'run_id': run_id,
    'archive_bytes': size,
    'last_stage': stages[-1].get('name') if stages else None,
    'uploads': uploaded,
    'scp_read_bytes': sorted(read_positions),
    'timing': timing,
}))
'@

Write-Host "Watching $RunId on production VM117; Ctrl+C stops this watcher only."
while ($true) {
    try {
        $raw = $probe | & ssh -T -o BatchMode=yes -o StrictHostKeyChecking=yes root@192.168.111.3 "qm guest exec 117 --pass-stdin --timeout 25 -- python3 - $RunId"
        if ($LASTEXITCODE -ne 0) { throw "SSH/QGA exited $LASTEXITCODE" }
        $guest = $raw | ConvertFrom-Json
        if ($guest.exitcode -ne 0 -or $guest.'out-truncated' -ne 0) { throw "Guest probe failed: $($guest.exitcode)" }
        $state = $guest.'out-data' | ConvertFrom-Json
        $size = [long]$state.archive_bytes
        $phase = if ($state.last_stage) { $state.last_stage } else { 'starting' }
        $summary = "[$($state.time)] last completed: $phase; archive $([math]::Round($size / 1MB, 1)) MiB"
        if ($size -gt 0 -and $state.scp_read_bytes.Count -gt 0) {
            $progress = @($state.scp_read_bytes | ForEach-Object { "{0:N1}%" -f (100 * [double]$_ / $size) })
            $summary += "; SCP file read $($progress -join ', ')"
        }
        foreach ($name in @('upload_primary', 'upload_secondary')) {
            $item = $state.uploads.$name
            if ($null -ne $item) {
                $result = if ($item.exit_code -eq 0) { 'done' } else { "failed($($item.exit_code))" }
                $summary += "; $name $result"
            }
        }
        if ($null -ne $state.timing) {
            $summary += "; backup $($state.timing.status)"
            if ($state.timing.failed_stage) { $summary += " at $($state.timing.failed_stage)" }
        }
        Write-Host $summary
        if ($Once) { exit 0 }
        if ($null -ne $state.timing) {
            if ($state.timing.status -eq 'ok') { exit 0 }
            exit 1
        }
    } catch {
        Write-Warning "Probe unavailable: $($_.Exception.Message)"
    }
    Start-Sleep -Seconds $IntervalSeconds
}
