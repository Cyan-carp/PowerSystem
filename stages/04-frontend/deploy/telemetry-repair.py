#!/usr/bin/env python3
"""Scoped forward repair of the retained, migrated TR-S04 transaction."""
import os
from pathlib import Path
import shutil

ENV = 'POWERSYSTEM_TELEMETRY_REPAIR_EVIDENCE'
FAILED = 'tr-s04-20261028-r3'
REPAIR = 'tr-s04-20261028-r4'
FORMAT = 'telemetry-frozen-repair/v1'


def candidate_binding(ps, manifest, value):
    expected=value.get('repair_images',{})
    ps.require(set(expected)=={'api','gateway'} and
               all(manifest['images'].get(n)==image for n,image in expected.items()) and
               {r['source_sha256'] for r in manifest['source_builds']}=={value.get('repair_source_sha256')} and
               {r['service'] for r in manifest['source_builds']}=={'api','gateway'},
               'telemetry_repair_tested_images_unbound')
    evidence=value.get('ack_evidence',{})
    path=ps.owned(evidence.get('path',''))
    ps.require(path.is_relative_to(ps.ROOT/'runtime') and ps.sha(path)==evidence.get('sha256'),
               'telemetry_repair_ack_evidence_changed')
    actual=ps.read(path)
    ps.require(actual.get('passed') is True and actual.get('sources_unchanged') is True and
               actual.get('api_image')==expected['api'] and
               actual.get('source_sha256')==value['repair_source_sha256'] and
               actual.get('binary_sha256')==evidence.get('binary_sha256'),
               'telemetry_repair_ack_binary_unbound')


def backup_receipt(ps, candidate, manifest, archive):
    frozen=candidate/'telemetry-repair.json'
    ps.require(ps.sha(frozen)==manifest['files'].get(frozen.name)==manifest['telemetry_repair']['sha256'],
               'telemetry_repair_frozen_proof_changed')
    value=ps.read(frozen);candidate_binding(ps,manifest,value)
    archive=Path(archive).resolve();baseline=ps.verified_full_baseline(archive)
    receipt=ps.read(Path(str(archive)+'.v4-maintenance.json'))
    ps.require(archive.parent==ps.BACKUP_DIR and receipt.get('archive')==archive.name and
               receipt.get('sha256')==baseline['sha256'] and
               receipt.get('frozen_repair_sha256')==ps.sha(frozen) and
               receipt.get('repair_candidate')==REPAIR and receipt.get('active')==value['active'] and
               receipt.get('before')==manifest['before']==value['states'] and
               receipt.get('telemetry',{}).get('kind')==FORMAT and
               receipt['telemetry'].get('phase')=='preserved_frozen' and
               ps.datetime.datetime.fromisoformat(receipt['started_at']).timestamp()>=
               ps.datetime.datetime.fromisoformat(manifest['created_at']).timestamp() and
               archive.stat().st_mtime>=ps.datetime.datetime.fromisoformat(manifest['created_at']).timestamp(),
               'telemetry_repair_new_backup_unbound')
    ps.maintenance_states_match(receipt['before'],receipt['after'])
    return receipt


def proof(ps, path=None):
    path = ps.owned(path or os.environ.get(ENV, ''))
    ps.require(path.is_relative_to(ps.ROOT/'runtime'), 'telemetry_repair_proof_scope')
    value = ps.read(path)
    ps.require(value.get('format') == FORMAT and value.get('candidate') == REPAIR and
               value.get('failed_candidate') in (FAILED,REPAIR) and value.get('owner_instruction') == '继续完成任务',
               'telemetry_repair_proof_invalid')
    return path, value


def admit(ps, path=None, states_after=None, v4_paused=False):
    path, value = proof(ps, path)
    state = ps.STATE_DIR
    ps.require(ps.read(state/'active.json') == value['active'] and
               ps.sha(state/'pending.json') == value['pending_sha256'] and
               ps.sha(state/'telemetry-forward-recovery-required.json') == value['recovery_sha256'],
               'telemetry_repair_transaction_changed')
    transaction = ps.read(state/'pending.json')
    ps.require(transaction['candidate'] == value['failed_candidate'] and transaction['previous'] == value['active']['release'] and
               set(transaction['services']) == {'api','gateway','prometheus','alertmanager'},
               'telemetry_repair_transaction_invalid')
    failed = ps.release(value['failed_candidate']); manifest = ps.immutable(failed)
    ps.require(ps.digest(manifest) == value['failed_manifest_sha256'], 'telemetry_repair_failed_candidate_changed')
    recovery = ps.read(state/'telemetry-forward-recovery-required.json')
    ps.require(recovery.get('phase') == 'frozen' and not recovery.get('errors') and
               recovery.get('legacy_worker_started') is False and recovery.get('maintenance_enabled') is True,
               'telemetry_repair_stop_unproven')
    rows = ps.inspect()
    expected = states_after or value['states']
    ps.require(ps.states(rows) == expected, 'telemetry_repair_instance_drift')
    if states_after is not None:
        ps.maintenance_states_match(value['states'], states_after)
    for name in ('api','gateway'):
        row = rows[name]
        labels = row['Config']['Labels']
        ps.require(not row['State']['Running'] and row['State']['ExitCode'] == 0 and
                   not row['State'].get('OOMKilled') and row['RestartCount'] == 0 and
                   row['Image'] == manifest['images'][name] and
                   labels.get('com.docker.compose.project') == ps.PROJECT and
                   labels.get('com.docker.compose.service') == name and
                   labels.get('com.docker.compose.project.config_files') == str(failed/'private/compose.json'),
                   'telemetry_repair_writer_changed')
    omitted = {'api','gateway'} | ({'agent-rag','ops-agent'} if v4_paused else set())
    ps.healthy({n:r for n,r in rows.items() if n not in omitted})
    ps.require(ps.telemetry_protocol().gate(ps.telemetry_context(),rows) == value['freeze_gate'] and
               value['freeze_gate']['enabled'] is True and
               ps.telemetry_protocol().schema(ps.telemetry_context(),rows)['s04'],
               'telemetry_repair_schema_or_gate_changed')
    migration = ps.read(failed/'telemetry-transition-state.json')
    ps.require(migration.get('phase') == 'ready_for_workers' and
               ps.telemetry_protocol().stop_owned_migrator(ps.telemetry_context(),failed,migration)['stop_proven'],
               'telemetry_repair_migrator_unproven')
    return path, value, rows


def backup_context(ps, v4_paused=False, states_after=None):
    ps.require(bool(os.environ.get(ENV)), 'pending_release_blocks_backup')
    return admit(ps, v4_paused=v4_paused, states_after=states_after)


def prepare(ps, args):
    path, value, rows = admit(ps, args.evidence)
    ps.require(args.id == REPAIR and value['failed_candidate']==FAILED, 'telemetry_repair_candidate_invalid')
    services = {'api','gateway','prometheus','alertmanager'}
    overrides = dict(x.split('=',1) for x in args.image)
    ps.require(len(args.image)==2 and set(overrides) == {'api','gateway'} and
               overrides==value.get('repair_images'), 'telemetry_repair_images_required')
    parent, _ = ps.active()
    candidate, manifest = ps.freeze(REPAIR,parent/'private/origin-args.json',parent.name,services,overrides,
        telemetry_evidence=args.telemetry_transition, frozen_repair=path)
    candidate_binding(ps,manifest,value)
    ps.require(ps.states(ps.inspect()) == value['states'], 'telemetry_repair_prepare_changed_runtime')
    return candidate, manifest


def publish(ps, args):
    candidate = ps.release(args.id); m = ps.immutable(candidate)
    ps.require(args.id == REPAIR and m.get('telemetry_repair'), 'telemetry_repair_candidate_invalid')
    frozen = candidate/'telemetry-repair.json'
    archive = Path(args.backup).resolve()
    receipt = backup_receipt(ps,candidate,m,archive)
    _, value, rows = admit(ps,frozen,states_after=receipt['after'])
    ps.require(receipt['active'] == value['active'] and receipt['before'] == m['before'] == value['states'] and
               receipt.get('telemetry',{}).get('kind') == FORMAT and
               receipt['telemetry'].get('phase') == 'preserved_frozen', 'telemetry_repair_backup_freeze_unproven')
    ps.preflight(ps.read(candidate/'private/compose.json'),m['origin_environment']);ps.drain()
    pending = ps.STATE_DIR/'pending.json'; recovery = ps.STATE_DIR/'telemetry-forward-recovery-required.json'
    for original,saved,expected in ((pending,candidate/'previous-pending.json',value['pending_sha256']),
                                   (recovery,candidate/'previous-recovery.json',value['recovery_sha256'])):
        if saved.exists():ps.require(ps.sha(saved)==expected,'telemetry_repair_prior_evidence_changed')
        else:shutil.copyfile(original,saved)
    transaction = {'previous':m['parent'],'candidate':REPAIR,'services':m['services'],
        'before':ps.states(rows),'backup_tier':'full','predeployment_backup':archive.name,
        'backup_sha256':receipt['sha256'],'started_at':ps.now(),
        'repair_of':FAILED,'previous_pending_sha256':value['pending_sha256']}
    document = ps.telemetry_transition().frozen(ps.telemetry_context(),candidate,m)
    journal = candidate/'repair-result.json'
    ps.require(not journal.exists(),'telemetry_repair_attempt_already_recorded')
    preserved={'previous_pending_sha256':ps.sha(candidate/'previous-pending.json'),
               'previous_recovery_sha256':ps.sha(candidate/'previous-recovery.json')}
    ps.require(preserved['previous_pending_sha256']==value['pending_sha256'] and
               preserved['previous_recovery_sha256']==value['recovery_sha256'],
               'telemetry_repair_prior_evidence_changed')
    ps.write(journal,{'passed':False,'phase':'starting','started_at':ps.now(),'repair_of':FAILED,**preserved})
    try:
        ps.write(pending,transaction)
        ps.write(candidate/'telemetry-transition-state.json',{'format':1,'phase':'ready_for_workers',
            'kind':'inherited_completed_schema','from_candidate':FAILED,
            'original_state_sha256':ps.sha(ps.release(FAILED)/'telemetry-transition-state.json')})
        ps.telemetry_monitoring().create_silence(ps.telemetry_context(),candidate,document)
        ps.telemetry_protocol().change_gate(ps.telemetry_context(),rows,value['freeze_gate'],False)
        ps.deploy(candidate,m['services'])
        accepted = ps.gate(candidate,m['services'],transaction['before'])
        ps.telemetry_monitoring().cleanup_silence(ps.telemetry_context(),candidate)
        ps.require(ps.read(pending) == transaction,'telemetry_repair_pending_changed')
        ps.require(ps.sha(candidate/'previous-pending.json')==preserved['previous_pending_sha256'] and
                   ps.sha(candidate/'previous-recovery.json')==preserved['previous_recovery_sha256'],
                   'telemetry_repair_prior_evidence_changed')
        shutil.copyfile(recovery,candidate/'recovery-before-accept.json')
        ps.set_active(candidate,accepted);pending.unlink()
        ps.write(recovery,{'phase':'resolved','candidate':REPAIR,'at':ps.now(),
            'previous_recovery_sha256':ps.sha(candidate/'recovery-before-accept.json')})
        ps.write(journal,{'passed':True,'phase':'accepted','at':ps.now(),'gate_file':accepted,'repair_of':FAILED,**preserved})
    except BaseException:
        try:
            rows=ps.inspect()
            try:installed=ps.telemetry_protocol().schema(ps.telemetry_context(),rows)
            except Exception:installed={'state':'unknown'}
            ps.telemetry_protocol().halt_forward_recovery(ps.telemetry_context(),rows,transaction,
                installed,'telemetry_forward_repair_failed')
        except Exception:
            pass
        finally:
            ps.telemetry_monitoring().cleanup_silence(ps.telemetry_context(),candidate,strict=False)
            actual=ps.read(recovery) if recovery.exists() else {}
            ps.write(journal,{'passed':False,'phase':'frozen' if actual.get('phase')=='frozen' else 'recovery_unproven',
                'at':ps.now(),'repair_of':FAILED,**preserved})
        raise ps.Blocked('telemetry_forward_repair_failed_frozen') from None
