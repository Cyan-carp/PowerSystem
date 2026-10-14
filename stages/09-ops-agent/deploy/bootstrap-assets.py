"""Host deployment catalog bootstrap; no task/JWT/approval creation or execution."""
import argparse,json,os,re,subprocess,sys
from pathlib import Path
ROOT=Path('/opt/powersystem')
def command(argv,**kw):
    r=subprocess.run(argv,capture_output=True,**kw)
    if r.returncode:raise RuntimeError('deployment_catalog_command_failed')
    return r.stdout
def sql(text):
    return command(['docker','exec','-i','powersystem-stage4-postgres-1','psql','-U','powersystem','-d','ma_ops','-v','ON_ERROR_STOP=1','-Atq'],input=text.encode()).decode().strip()
def bootstrap_sql(assets,registry_hash):
    payload=json.dumps(assets,ensure_ascii=False).replace("'","''")
    return """BEGIN;
SELECT pg_advisory_xact_lock(90903);
DO $$BEGIN IF EXISTS(SELECT 1 FROM ma_tasks) OR EXISTS(SELECT 1 FROM ma_assets) OR EXISTS(SELECT 1 FROM ma_asset_versions) THEN RAISE EXCEPTION 'fresh_task_database_required'; END IF; END$$;
CREATE TEMP TABLE imported_assets AS SELECT * FROM jsonb_to_recordset('"""+payload+"""'::jsonb) AS x(asset_id text,kind text,display_name text,project_scope text,parent_id text,enabled boolean,criticality text,environment text,version integer,verified_at text,connection_ref text,identity_hash text,capabilities jsonb);
INSERT INTO ma_assets(asset_id,kind,display_name,project_scope,parent_id,enabled,criticality,environment,version,verified_at,connection_ref,identity_hash,capabilities,registry_hash)
SELECT asset_id,kind,display_name,project_scope,parent_id,enabled,criticality,environment,version,verified_at::timestamptz,connection_ref,identity_hash,capabilities,'"""+registry_hash+"""' FROM imported_assets ORDER BY kind!='host';
INSERT INTO ma_asset_versions(asset_id,version,snapshot,imported_by)
SELECT asset_id,version,to_jsonb(imported_assets),0 FROM imported_assets;
COMMIT;
"""
def main(a):
    r=a.release.resolve()
    if os.uname().nodename!='R730XD' or not r.is_relative_to(ROOT/'runtime') or not re.fullmatch('ma-m4-[a-z0-9-]{1,65}',r.name) or not (r/'database-created.json').is_file():raise ValueError('owned_new_task_database_required')
    sys.path.insert(0,str(r/'candidate/stages/09-ops-agent'))
    from ops_contract import validate_catalog,digest
    installation=json.loads((r/'private/collector-before-installation.json').read_text())
    if installation['owner']!='powersystem-ma-m2':raise ValueError('established_collector_required')
    assets=json.loads((r/'private/collector-before-registry.json').read_text())['assets']
    validate_catalog(assets);h=digest(assets)
    if installation['registry_hash']!=h:raise ValueError('collector_baseline_digest_mismatch')
    if (r/'catalog-bootstrap.json').exists():raise ValueError('catalog_bootstrap_already_recorded')
    sql(bootstrap_sql(assets,h))
    (r/'catalog-bootstrap.json').write_text(json.dumps({'assets':len(assets),'registry_hash':h,'source':'prepared established collector','actor':'owner-authorized host release program','imported_by':0,'external_starting_versions':{a['asset_id']:a['version'] for a in assets},'tasks_created':0,'credentials_issued':0,'approvals_created':0},indent=2))
    print(json.dumps({'catalog_bootstrapped':True,'assets':len(assets),'tasks_created':0}))
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--release',type=Path,required=True);main(p.parse_args())
