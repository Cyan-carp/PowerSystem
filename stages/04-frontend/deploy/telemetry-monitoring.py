#!/usr/bin/env python3
"""Versioned telemetry rules, a local-only receiver, and an owned planned silence."""
import datetime
import hashlib
import json
import re
from pathlib import Path
import urllib.request


TARGETS = {'prometheus': '/etc/prometheus/alerts.yml',
           'alertmanager': '/etc/alertmanager/alertmanager.yml'}


def render_alertmanager(original):
    route = ('    - receiver: telemetry-local-only\n'
             '      matchers:\n'
             '        - component="telemetry"\n'
             '        - alertname=~"PowerSystemTelemetry.*"\n')
    if (original.count('  routes:\n') != 1 or original.count('\nreceivers:\n') != 1 or
            'telemetry-local-only' in original):
        raise ValueError('telemetry_alertmanager_parent_unsupported')
    return original.replace('  routes:\n', '  routes:\n'+route, 1).replace(
        '\nreceivers:\n', '\nreceivers:\n  - name: telemetry-local-only\n', 1)


def prepare(ps, candidate, document, config, rows):
    evidence = document.get('monitoring', {})
    source = ps.ROOT/'runtime/source-builds/contexts'/document['source_sha256']
    alerts = (source/'stages/04-frontend/deploy/alerts.yml').read_bytes()
    ps.require(hashlib.sha256(alerts).hexdigest() == evidence.get('rules_sha256'),
               'telemetry_rules_source_unbound')
    actual = {}
    for service, target in TARGETS.items():
        mounts = [m for m in rows[service]['Mounts'] if m['Destination'] == target]
        ps.require(len(mounts) == 1 and mounts[0]['RW'] is False, 'telemetry_monitoring_mount_invalid')
        p = Path(mounts[0]['Source'])
        ps.require(p.is_file() and not p.is_symlink() and ps.sha(p) == evidence.get(service+'_parent_sha256'),
                   'telemetry_monitoring_parent_drift:'+service)
        actual[service] = p
    original = actual['alertmanager'].read_text(encoding='utf-8')
    try:
        if 'telemetry-local-only' in original:
            failed = ps.release('tr-s04-20261028-r3')
            relative = 'telemetry-monitoring/alertmanager.yml'
            ps.require(candidate.name == 'tr-s04-20261028-r4' and
                       actual['alertmanager'] == failed/relative and
                       ps.sha(actual['alertmanager']) == ps.read(failed/'manifest.json')['files'].get(relative),
                       'telemetry_existing_receiver_unbound')
            manager = original
        else:
            manager = render_alertmanager(original)
    except ValueError as error:
        raise ps.Blocked(str(error)) from None
    ps.require(evidence.get('new_alertmanager_sha256') == hashlib.sha256(manager.encode()).hexdigest(),
               'telemetry_local_receiver_unbound')
    blobs = {'prometheus': alerts, 'alertmanager': manager.encode()}
    files = []
    for service, content in blobs.items():
        relative = 'telemetry-monitoring/'+('alerts.yml' if service == 'prometheus' else 'alertmanager.yml')
        path = candidate/relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(0o644)  # Only these public configurations are visible to the service UID.
        volumes = [v for v in config['services'][service]['volumes'] if v['target'] == TARGETS[service]]
        ps.require(len(volumes) == 1 and volumes[0].get('read_only') is True,
                   'telemetry_monitoring_bind_required')
        volumes[0]['source'] = str(path)
        files.append(relative)
        executable, action = ('/bin/promtool', ['check', 'rules']) if service == 'prometheus' else (
                              '/bin/amtool', ['check-config'])
        args = ['docker', 'run', '--rm', '--pull', 'never', '--network', 'none', '--read-only',
                '--cpus', '1', '--memory', '128m', '--entrypoint', executable,
                '--mount', 'type=bind,source='+str(path)+',target=/review.yml,readonly']
        # amtool can inspect existing credential-file references without printing values.
        if service == 'alertmanager':
            for mount in rows[service]['Mounts']:
                if mount['Destination'].startswith('/run/') and not mount['RW']:
                    args += ['--mount', 'type=bind,source='+mount['Source']+',target='+mount['Destination']+',readonly']
        ps.command(args+[rows[service]['Image'], *action, '/review.yml'])
    return files


def inherit(ps, parent, config, rows, services):
    if parent is None:
        return
    previous = ps.read(ps.release(parent)/'private/compose.json')
    for service, target in TARGETS.items():
        if service not in rows and service not in config['services']:
            continue
        if service in services:
            continue
        expected = [v for v in previous['services'].get(service, {}).get('volumes', []) if v['target'] == target]
        current = [v for v in rows.get(service, {}).get('Mounts', []) if v['Destination'] == target]
        planned = [v for v in config['services'].get(service, {}).get('volumes', []) if v['target'] == target]
        ps.require(len(expected) == len(current) == len(planned) == 1, 'telemetry_monitoring_mount_missing')
        expected, current, planned = expected[0], current[0], planned[0]
        ps.require(expected['source'] == current['Source'], 'telemetry_monitoring_running_mount_drift')
        planned['source'] = expected['source']


def endpoint(rows):
    networks = rows['alertmanager']['NetworkSettings']['Networks']
    addresses = [n['IPAddress'] for n in networks.values() if n.get('IPAddress')]
    if len(addresses) != 1:
        raise ValueError('telemetry_alertmanager_network_ambiguous')
    return 'http://'+addresses[0]+':9093'


def api_instance(ps, rows):
    networks = rows['prometheus']['NetworkSettings']['Networks']
    addresses = [n['IPAddress'] for n in networks.values() if n.get('IPAddress')]
    ps.require(len(addresses) == 1, 'telemetry_prometheus_network_ambiguous')
    with urllib.request.urlopen('http://'+addresses[0]+':9090/api/v1/targets?state=active', timeout=5) as response:
        targets = json.load(response)
    instances = {t.get('labels', {}).get('instance') for t in targets.get('data', {}).get('activeTargets', [])
                 if t.get('labels', {}).get('job') == 'api'}
    ps.require(len(instances) == 1 and None not in instances, 'telemetry_api_target_ambiguous')
    return instances.pop()


def silence_file(candidate, attempt=''):
    if attempt and not re.fullmatch(r'resume-r[1-3]', attempt):
        raise ValueError('telemetry_silence_attempt_invalid')
    return candidate/('telemetry-planned-silence'+('-'+attempt if attempt else '')+'.json')


def create_silence(ps, candidate, document, attempt=''):
    path = silence_file(candidate, attempt)
    ps.require(not path.exists(), 'telemetry_silence_already_recorded')
    rows = ps.inspect()
    instance = api_instance(ps, rows)
    proof_sha = ps.sha(candidate/'telemetry-transition.json')
    source_sha = document['source_sha256']
    start = datetime.datetime.now(datetime.timezone.utc)
    start = start.replace(microsecond=(start.microsecond//1000)*1000)
    end = start+datetime.timedelta(minutes=30)
    body = {'matchers': [{'name': 'alertname', 'value': 'PowerSystemAPIDown|PowerSystemTelemetry.*',
                          'isRegex': True, 'isEqual': True},
                         {'name': 'job', 'value': 'api', 'isRegex': False, 'isEqual': True},
                         {'name': 'instance', 'value': instance, 'isRegex': False, 'isEqual': True}],
            'startsAt': start.isoformat(), 'endsAt': end.isoformat(),
            'createdBy': 'powersystem-release:'+candidate.name+':'+proof_sha+(':'+attempt if attempt else ''),
            'comment': 'Planned telemetry migration; source='+source_sha+'; proof='+proof_sha}
    # Persist ownership before POST, so an ambiguous response still leaves a
    # bounded, identifiable recovery instruction instead of an untracked silence.
    receipt = {'id': None, 'created_by': body['createdBy'], 'starts_at': body['startsAt'],
               'expires_at': body['endsAt'], 'candidate': candidate.name, 'removed': False,
               'matchers': body['matchers'], 'comment': body['comment'],
               'source_sha256': source_sha, 'proof_sha256': proof_sha, 'attempt': attempt}
    ps.write(path, receipt)
    request = urllib.request.Request(endpoint(rows)+'/api/v2/silences',
              data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(request, timeout=5) as response:
        value = json.load(response)
    ps.require(isinstance(value.get('silenceID'), str), 'telemetry_silence_receipt_missing')
    receipt['id'] = value['silenceID']
    ps.write(path, receipt)
    return receipt


def remove_silence(ps, candidate, attempt=''):
    path = silence_file(candidate, attempt)
    if not path.exists():
        return
    value = ps.read(path)
    if value.get('removed'):
        return
    proof_sha = ps.sha(candidate/'telemetry-transition.json')
    proof = ps.read(candidate/'telemetry-transition.json')
    ps.require(value.get('created_by') == 'powersystem-release:'+candidate.name+':'+proof_sha+(':'+attempt if attempt else '') and
               value.get('candidate') == candidate.name and value.get('proof_sha256') == proof_sha and
               value.get('source_sha256') == proof.get('source_sha256'), 'telemetry_silence_not_owned')
    base = endpoint(ps.inspect())
    if value.get('id') is None:
        with urllib.request.urlopen(base+'/api/v2/silences', timeout=5) as response:
            matches = [v for v in json.load(response) if v.get('createdBy') == value['created_by'] and
                       v.get('comment') == value['comment'] and v.get('matchers') == value['matchers'] and
                       v.get('status', {}).get('state') != 'expired']
        ps.require(len(matches) <= 1, 'telemetry_silence_identity_ambiguous')
        if not matches:
            value.update(removed=True, removal_result='owned_silence_absent')
            ps.write(path, value)
            return
        value['id'] = matches[0]['id']
        ps.write(path, value)
    url = base+'/api/v2/silence/'+value['id']
    with urllib.request.urlopen(url, timeout=5) as response:
        current = json.load(response)
    ps.require(current.get('createdBy') == value['created_by'] and
               current.get('matchers') == value['matchers'] and current.get('comment') == value['comment'],
               'telemetry_silence_identity_changed')
    if current.get('status', {}).get('state') == 'expired':
        value.update(removed=True, removal_result='owned_silence_expired')
        ps.write(path, value)
        return
    # Alertmanager persists milliseconds. Older receipts may contain micros.
    ps.require(int(datetime.datetime.fromisoformat(current['endsAt'].replace('Z', '+00:00')).timestamp()*1000) ==
               int(datetime.datetime.fromisoformat(value['expires_at']).timestamp()*1000), 'telemetry_silence_expiry_changed')
    with urllib.request.urlopen(urllib.request.Request(url, method='DELETE'), timeout=5):
        pass
    with urllib.request.urlopen(url, timeout=5) as response:
        expired = json.load(response)
    ps.require(expired.get('status', {}).get('state') == 'expired', 'telemetry_silence_removal_unproven')
    value.update(removed=True, removed_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
    ps.write(path, value)


def cleanup_silence(ps, candidate, strict=True, attempt=''):
    silence_file(candidate, attempt)  # Validate before constructing any paths.
    failure = candidate/('telemetry-silence-recovery-required'+('-'+attempt if attempt else '')+'.json')
    for round_no in range(2):
        try:
            remove_silence(ps, candidate, attempt)
            if failure.exists():
                failure.unlink()
            return
        except Exception as error:
            ps.write(failure, {'passed': False, 'candidate': candidate.name,
                     'at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                     'error_type': type(error).__name__, 'attempt': round_no+1,
                     'action': 'Verify owned planned silence and expire it; preserve release pending'})
    if strict:
        raise ps.Blocked('telemetry_planned_silence_cleanup_requires_recovery')
