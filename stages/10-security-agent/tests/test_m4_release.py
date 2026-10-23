"""Public package and production deployment boundary regression, no host mutations."""
import importlib.util,io,json,tarfile
from pathlib import Path
import pytest,yaml
ROOT=Path(__file__).resolve().parents[3]
def module():
    spec=importlib.util.spec_from_file_location('ma_release_test',ROOT/'stages/09-ops-agent/deploy/ma-release.py')
    result=importlib.util.module_from_spec(spec);spec.loader.exec_module(result);return result
def tar(tmp_path,name,symlink=False):
    path=tmp_path/'candidate.tar.gz'
    with tarfile.open(path,'w:gz') as a:
        m=tarfile.TarInfo(name)
        if symlink:m.type=tarfile.SYMTYPE;m.linkname='/etc/passwd';a.addfile(m)
        else:m.size=1;a.addfile(m,io.BytesIO(b'x'))
    return path
@pytest.mark.parametrize('name',['../../etc/passwd','/etc/passwd','artifacts/private/config.env','stages/09-ops-agent/.env','stages/04-frontend/node_modules/unsafe.js','unexpected.py'])
def test_C32_package_traversal_and_private_files_blocked(tmp_path,name):
    with pytest.raises(ValueError):list(module().archive_members(tar(tmp_path,name)))
def test_C32_symlink_blocked(tmp_path):
    with pytest.raises(ValueError):list(module().archive_members(tar(tmp_path,'stages/09-ops-agent/unsafe',True)))
def test_C32_public_source_accepted(tmp_path):
    assert list(module().archive_members(tar(tmp_path,'stages/09-ops-agent/ops_agent/main.py')))[0][1]==b'x'
def test_C34_new_services_internal_nonroot_and_separate_secrets():
    value=yaml.safe_load((ROOT/'stages/09-ops-agent/deploy/production.compose.yaml').read_text())
    for name in ('ops-agent','security-agent','ops-executor'):
        service=value['services'][name]
        assert not service.get('ports') and service['read_only'] and service['user'].split(':')[0]!='0'
        assert service['cap_drop']==['ALL'] and service['security_opt']==['no-new-privileges:true']
        assert not any('docker.sock' in v for v in service['volumes'])
    assert len({value['services'][n]['volumes'][0].split(':')[0] for n in ('ops-agent','security-agent','ops-executor')})==3
def test_C32_unknown_existing_task_database_not_adopted(tmp_path,monkeypatch):
    m=module();r=tmp_path/'release';(r/'private/ops').mkdir(parents=True);(r/'private/ops/db-password').write_text('A'*40)
    calls=[]
    def fake(argv,**kw):calls.append((argv,kw));return b'1'
    monkeypatch.setattr(m,'command',fake)
    with pytest.raises(ValueError,match='unowned_ma_ops'):m.migrate(r)
    assert len(calls)==1 and 'SELECT count(*)' in calls[0][0][-1]
def test_C32_migration_password_only_in_stdin_and_dedicated_database(tmp_path,monkeypatch):
    m=module();r=tmp_path/'release';(r/'private/ops').mkdir(parents=True);(r/'private/ops/db-password').write_text('A'*40)
    deploy=r/'candidate/stages/09-ops-agent/deploy';deploy.mkdir(parents=True)
    for name in ('001_tasks.sql','002_assets.sql','004_approvals.sql','005_security_reviews.sql','006_security_gates.sql','007_runtime_context.sql'):(deploy/name).write_text('SELECT 1;\n')
    calls=[]
    def fake(argv,**kw):calls.append((argv,kw));return b'0' if len(calls)==1 else b''
    monkeypatch.setattr(m,'command',fake);m.migrate(r)
    assert 'A'*40 not in str([argv for argv,_ in calls])
    assert b'CREATE DATABASE ma_ops' in calls[1][1]['input']
    assert calls[2][0][calls[2][0].index('-d')+1]=='ma_ops'
def test_C32_rollback_changes_only_selected_services(tmp_path):
    m=module();args=m.compose(tmp_path,True)
    assert str(m.ROOT/m.OVERLAY) not in args
    assert m.BASE_SERVICES==('api','frontend','agent-rag')
    assert m.NEW_SERVICES==('ops-agent','security-agent','ops-executor')

def test_C32_actual_source_rollback_restores_old_and_preserves_new_in_quarantine(tmp_path,monkeypatch):
    m=module();root=tmp_path/'project';root.mkdir();monkeypatch.setattr(m,'ROOT',root)
    r=tmp_path/'release';(r/'private').mkdir(parents=True)
    old=root/'stages/09-ops-agent/old.py';old.parent.mkdir(parents=True);old.write_bytes(b'new candidate')
    new=old.parent/'new.py';new.write_bytes(b'newly added')
    with tarfile.open(r/'private/source-before.tar.gz','w:gz') as ar:
        item=tarfile.TarInfo('stages/09-ops-agent/old.py');item.size=12;item.mode=0o644;ar.addfile(item,io.BytesIO(b'old baseline'))
    (r/'baseline.json').write_text(json.dumps({'new_paths':['stages/09-ops-agent/new.py']}))
    m.restore_source(r)
    assert old.read_bytes()==b'old baseline' and not new.exists()
    assert (r/'private/new-source-on-rollback/stages/09-ops-agent/new.py').read_bytes()==b'newly added'

def test_C32_new_source_outside_root_never_moved(tmp_path,monkeypatch):
    m=module();monkeypatch.setattr(m,'ROOT',tmp_path/'project');m.ROOT.mkdir()
    with pytest.raises(ValueError):m.source_target('../outside')

def test_C32_restricted_collector_key_removed_and_restored_without_other_keys(tmp_path,monkeypatch):
    m=module();auth=tmp_path/'authorized_keys';auth.write_text('existing restricted key\n');monkeypatch.setattr(m,'COLLECTOR_AUTH',auth)
    r=tmp_path/'release';(r/'private/executor').mkdir(parents=True);(r/'private/executor/collector-ed25519.pub').write_text('ssh-ed25519 AAAA synthetic-fixture')
    m.collector_authorization(r,True);assert len(auth.read_text().splitlines())==2
    m.collector_authorization(r,False);assert auth.read_text()=='existing restricted key\n'
    m.collector_authorization(r,True);assert 'restrict,command=' in auth.read_text() and 'existing restricted key' in auth.read_text()


def test_C35_original_business_and_candidate_health_are_both_required():
    spec=importlib.util.spec_from_file_location('ma_observe_test',ROOT/'stages/09-ops-agent/deploy/observe-ma.py')
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    baseline={s:{'image':s,'running':True,'health':'healthy'} for s in m.SERVICES}
    baseline['gateway']={'image':'old-business','running':True,'health':None}
    pins={s:s for s in m.SERVICES}
    assert m.candidate_healthy(baseline,pins) and m.business_healthy(baseline)
    baseline['gateway']['running']=False
    assert m.candidate_healthy(baseline,pins) and not m.business_healthy(baseline)
    baseline['gateway']['running']=True;baseline['security-agent']['image']='other-candidate'
    assert not m.candidate_healthy(baseline,pins)


def test_C32_asset_versions_remain_monotonic_after_actual_rollback():
    m=module()
    assert m.advanced_asset_version(2,2,True)==3
    assert m.advanced_asset_version(2,3,True)==4
    assert m.advanced_asset_version(0,1,True)==2


def test_C32_failed_asset_import_does_not_consume_committed_versions():
    m=module()
    assert m.advanced_asset_version(3,2,True)==3
    assert m.advanced_asset_version(9,2,True)==3
    assert m.advanced_asset_version(3,2,False)==3

def test_C32_external_baseline_catalog_bootstrap_is_fresh_only_and_no_invented_history():
    spec=importlib.util.spec_from_file_location('ma_bootstrap_test',ROOT/'stages/09-ops-agent/deploy/bootstrap-assets.py')
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    sql=m.bootstrap_sql([{'asset_id':'synthetic-fixture','version':2,'display_name':"quoted ' fixture"}],'a'*64)
    assert 'EXISTS(SELECT 1 FROM ma_tasks)' in sql and 'EXISTS(SELECT 1 FROM ma_assets)' in sql
    assert 'EXISTS(SELECT 1 FROM ma_asset_versions)' in sql
    assert "quoted '' fixture" in sql and 'credentials' not in sql
    assert 'to_jsonb(imported_assets),0' in sql


@pytest.mark.parametrize('after_id',['same-owned-container','unexpected-new-container'])
def test_C32_executor_reload_blocks_unexpected_identity_change(monkeypatch,after_id):
    m=module();rows=iter([{'Id':'same-owned-container'},{'Id':after_id,'State':{'Health':{'Status':'healthy'}}}]);calls=[]
    monkeypatch.setattr(m,'inspect',lambda service:next(rows))
    monkeypatch.setattr(m,'command',lambda argv:calls.append(argv))
    if after_id!='same-owned-container':
        with pytest.raises(ValueError,match='identity_changed'):m.reload_executor_registry()
    else:m.reload_executor_registry()
    assert calls==[['docker','restart','powersystem-stage4-ops-executor-1']]


def test_C32_second_actual_rollback_preserves_first_candidate_evidence(tmp_path,monkeypatch):
    m=module();root=tmp_path/'project';root.mkdir();monkeypatch.setattr(m,'ROOT',root)
    r=tmp_path/'release';(r/'private').mkdir(parents=True)
    new=root/'stages/09-ops-agent/new.py';new.parent.mkdir(parents=True);new.write_bytes(b'candidate first')
    with tarfile.open(r/'private/source-before.tar.gz','w:gz'):pass
    (r/'baseline.json').write_text(json.dumps({'new_paths':['stages/09-ops-agent/new.py']}))
    m.restore_source(r);new.write_bytes(b'candidate final');m.restore_source(r)
    assert (r/'private/new-source-on-rollback/stages/09-ops-agent/new.py').read_bytes()==b'candidate first'
    latest=json.loads((r/'source-restored.json').read_text())
    assert (Path(latest['quarantine_directory'])/'stages/09-ops-agent/new.py').read_bytes()==b'candidate final'
    assert len(list(r.glob('source-restored-history-*.json')))==1 and not new.exists()
