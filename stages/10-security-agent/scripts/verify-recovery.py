"""Two fresh-process phases around an operator-controlled isolated restart.

Never calls the model, truncates history, or restarts any container itself.
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path
from uuid import UUID

import asyncpg

ROOT=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(ROOT/'stages/10-security-agent'),str(ROOT/'stages/09-ops-agent')]
from ops_agent.reviews import ReviewController
from review_contract import ReviewRequest, digest
from samples import CASES, seed


class NoNetwork:
    def __init__(self):self.calls=0
    async def post(self,*args,**kwargs):
        self.calls+=1
        raise AssertionError('recovery_must_not_call_model')


async def snapshot(pool):
    reviews=await pool.fetch('SELECT review_id,state,request_hash,decision_hash,signature_validated FROM ma_security_reviews ORDER BY review_id')
    events=await pool.fetch('SELECT * FROM ma_security_review_events ORDER BY event_seq')
    return {'reviews':[dict(row) for row in reviews],
            'events_count':len(events),'events_hash':digest(json.loads(json.dumps([dict(row) for row in events],default=str)))}


async def main(args):
    run=(ROOT/args.run).resolve();out=(ROOT/args.output).resolve()
    if not run.is_relative_to(ROOT/'artifacts/stage10-security-agent') or not out.is_relative_to(run):
        raise ValueError('invalid_sec_output')
    private=run/'private';db=json.loads((private/'databases.json').read_text())
    if (db['host'],db['port'],db['acceptance_database'])!=('127.0.0.1',15432,'sec_m1_acceptance_test'):
        raise ValueError('isolated_acceptance_database_required')
    pool=await asyncpg.create_pool(host=db['host'],port=db['port'],user=db['user'],
        password=Path(db['password_file']).read_text().strip(),database=db['acceptance_database'],min_size=1,max_size=2)
    client=NoNetwork()
    keys=[(private/name).read_text().strip() for name in ('sec-service-token','sec-request-key','sec-result-key')]
    controller=ReviewController(pool,client,*keys)
    try:
        await controller.check_database()
        if args.phase=='prepare':
            out.mkdir(parents=True,exist_ok=False)
            if await pool.fetchval("SELECT count(*) FROM ma_security_reviews WHERE state='pending'"):
                raise ValueError('existing_pending_requires_separate_recovery')
            params=await seed(pool,CASES[0]);request=await controller.build(**params)
            await controller.reserve(request)
            before=await snapshot(pool)
            (out/'before.json').write_text(json.dumps({'snapshot':before,'params':params},default=str,indent=2),encoding='utf-8')
            print(json.dumps({'prepared_pending':1,'network_calls':client.calls,'completed_retained':sum(r['state']=='completed' for r in before['reviews'])}))
        else:
            report_file=out/'summary.json'
            if report_file.exists():raise ValueError('recovery_attempt_already_verified')
            before=json.loads((out/'before.json').read_text())
            current=json.loads(json.dumps(await snapshot(pool),default=str))
            if current!=before['snapshot']:raise ValueError('restart_changed_persisted_history')
            recovered=await controller.recover()
            params=before['params']
            for key in ('task_id','review_id'):params[key]=UUID(params[key])
            row=await controller.submit(**params)
            if recovered!=1 or row['state']!='unavailable' or row['error_code']!='review_interrupted':
                raise ValueError('invalid_pending_recovery')
            if row['decision'] is not None or row['signature_validated'] or client.calls:
                raise ValueError('recovery_created_effective_decision')
            after=await snapshot(pool)
            completed=[r for r in current['reviews'] if r['state']=='completed']
            for old in completed:
                saved=await controller.get(UUID(old['review_id']),100101)
                if saved['decision_hash']!=old['decision_hash'] or digest(saved['decision'])!=old['decision_hash']:
                    raise ValueError('completed_decision_changed')
            if after['events_count']!=current['events_count']+1:raise ValueError('missing_recovery_audit')
            summary={'database':'sec_m1_acceptance_test','restart_history_unchanged':True,
                'completed_retained':len(completed),'pending_recovered':recovered,'recovered_state':row['state'],
                'network_calls':client.calls,'effective_decision_created':False,'audit_event_added':1,'passed':True}
            report_file.write_text(json.dumps(summary,indent=2),encoding='utf-8')
            print(json.dumps(summary))
    finally:await pool.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--run',required=True)
    parser.add_argument('--output',required=True);parser.add_argument('--phase',choices=('prepare','verify'),required=True)
    asyncio.run(main(parser.parse_args()))
