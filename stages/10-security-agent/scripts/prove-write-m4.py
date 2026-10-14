"""Readonly corroboration: one human approval, one DB attempt, one broker action."""
import argparse,asyncio,json,subprocess
from pathlib import Path
import asyncpg
ROOT=Path(__file__).resolve().parents[3]
async def main(a):
    run=(ROOT/a.run).resolve();out=(ROOT/a.output).resolve();out.mkdir(exist_ok=False)
    if not out.is_relative_to(run) or not run.name.startswith('sec-m4-'):raise ValueError('owned_batch_required')
    env=dict(l.split('=',1) for l in (run/'private/local.env').read_text().splitlines() if '=' in l)
    if not env['OPS_DATABASE_URL'].endswith(':15435/sec_m4_acceptance_test'):raise ValueError('dedicated_database_required')
    report=json.loads((run/'write-terminal.json').read_text());approval=report['approvals'][0];attempt=report['execution_attempts'][0];data=attempt['evidence']['data']
    db=await asyncpg.connect(env['OPS_DATABASE_URL'])
    counts={'attempts':await db.fetchval('SELECT count(*) FROM ma_execution_attempts'),'consumed':await db.fetchval('SELECT count(*) FROM ma_approvals WHERE consumed_at IS NOT NULL'),'uncertain':await db.fetchval("SELECT count(*) FROM ma_execution_attempts WHERE status='execution_uncertain'")};await db.close()
    drill=json.loads((run/'drill.json').read_text());info=json.loads(subprocess.run(['docker','inspect',drill['container_id']],capture_output=True,check=True).stdout)[0]
    query="import sqlite3,json; print(json.dumps(sqlite3.connect('/state/attempts.sqlite').execute('SELECT approval_id,call_id FROM attempts').fetchall()))"
    rows=json.loads(subprocess.run(['docker','exec',drill['project']+'-broker-1','python','-c',query],capture_output=True,check=True).stdout)
    checks={'task_succeeded':report['status']=='succeeded','approval_decided':approval['status']=='approved' and bool(approval['decided_at']),'approval_consumed_once':counts['consumed']==1 and counts['attempts']==1 and bool(approval['consumed_at']),'broker_one_action':rows==[[approval['approval_id'],attempt['call_id']]],'same_new_container':data['before']['container_id']==data['after']['container_id']==drill['container_id'],'same_image':data['before']['image_digest']==data['after']['image_digest']==drill['image_digest'],'started_at_changed':data['before']['started_at']!=data['after']['started_at']==info['State']['StartedAt'],'currently_healthy':info['State']['Health']['Status']=='healthy','post_execution_allowed':any(x['kind']=='post_execution' and x['state']=='completed' and x['decision']=='allow' for x in report['security_reviews']),'no_uncertain':counts['uncertain']==0,'all_write_gates_closed':env['OPS_WRITE_ENABLED']=='false' and json.loads((run/'private/broker/broker.json').read_text())['write_enabled'] is False and json.loads((run/'private/broker/write-window.json').read_text())['write_enabled'] is False}
    (out/'checks.json').write_text(json.dumps({'checks':checks,'counts':counts,'task_id':report['task_id'],'approval_id':approval['approval_id'],'human_action_correlated_with_record':True},indent=2));print(json.dumps(checks))
    if not all(checks.values()):raise ValueError('one_shot_corroboration_failed')
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--run',required=True);p.add_argument('--output',required=True);asyncio.run(main(p.parse_args()))
