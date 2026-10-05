"""Loopback missing-model and bounded recovery acceptance, synthetic only."""
import asyncio
import importlib.util
import json
import sys
from pathlib import Path
import httpx
from redis.asyncio import Redis
from agent.config import Config
spec=importlib.util.spec_from_file_location('smoke',Path(__file__).with_name('smoke-v2m2.py'))
smoke=importlib.util.module_from_spec(spec);spec.loader.exec_module(smoke)
state=Path('artifacts/stage7/v2m2/unconfigured-20261001/state.json')

async def main(mode):
    cfg=Config.load();state.parent.mkdir(parents=True,exist_ok=True)
    admin=int(smoke.sql("SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1").splitlines()[0])
    async with httpx.AsyncClient(base_url='http://127.0.0.1:8080',headers={'Authorization':'Bearer '+smoke.jwt(admin)},timeout=50,trust_env=False) as api:
        if mode=='missing':
            assert not cfg.model_configured
            ids=[smoke.alarm(0),smoke.alarm(0,age_hours=48),smoke.alarm(0)]
            smoke.sql(f"UPDATE alarm_records SET status='recovered',recovered_at=now() WHERE id={ids[2]}")
            state.write_text(json.dumps({'ids':ids}),encoding='utf-8')
            for _ in range(60):
                raw=smoke.sql('SELECT json_agg(json_build_object(\'id\',alarm_id,\'task_status\',task_status,\'reason\',reason,\'attempts\',attempts,\'evidence\',evidence,\'result\',result)) FROM agent_interpretations WHERE alarm_id IN ('+','.join(map(str,ids))+')')
                rows=json.loads(raw) if raw else []
                if len(rows)==3 and all(row['task_status']=='degraded' for row in rows):break
                await asyncio.sleep(.5)
            assert len(rows)==3 and all(row['reason']=='not_configured' and row['attempts']==1 and row['evidence'] and row['result']['conclusion'] is None and not row['result']['suggestions'] for row in rows)
            status=(await api.get('/api/v1/agent/model-status')).json()['data']
            assert not status['available'] and status['reason']=='not_configured'
            health=(await api.get('http://127.0.0.1:8092/health')).json()
            assert health['status']=='ok' and health['model_configured'] is False
            assert (await api.post(f'/api/v1/alarms/{ids[0]}/ack')).status_code==200
            state.with_name('missing.json').write_text(json.dumps({'checks':{'three_facts_without_model':True,'no_retry':True,'health_is_liveness':True,'admin_state':True,'business_ack':True},'rows':rows},ensure_ascii=False,indent=2),encoding='utf-8')
            print('Missing model: 5 checks passed; events retain facts and human acknowledgment.')
        else:
            ids=json.loads(state.read_text())['ids']
            redis=Redis.from_url(cfg.redis_url,decode_responses=True);await redis.delete('stage7:model:probe-limit');await redis.aclose()
            assert (await api.post('/api/v1/agent/model-probe')).status_code==200
            for _ in range(60):
                rows=json.loads(smoke.sql('SELECT json_agg(json_build_object(\'id\',alarm_id,\'task_status\',task_status,\'attempts\',attempts,\'result\',result)) FROM agent_interpretations WHERE alarm_id IN ('+','.join(map(str,ids))+')'))
                recent=next(row for row in rows if row['id']==ids[0])
                if recent['task_status']=='completed':break
                await asyncio.sleep(.5)
            assert recent['result']['status']=='answered'
            assert all(row['task_status']=='degraded' and row['attempts']==1 for row in rows if row['id']!=ids[0])
            smoke.sql('UPDATE alarm_records SET status=\'recovered\',recovered_at=now() WHERE id IN ('+','.join(map(str,ids))+'); UPDATE devices SET deleted_at=now() WHERE id IN (SELECT device_id FROM alarm_records WHERE id IN ('+','.join(map(str,ids))+'));')
            state.with_name('recovery.json').write_text(json.dumps({'checks':{'recent_active_only':True,'history_not_replayed':True,'recovered_not_replayed':True},'rows':rows},ensure_ascii=False,indent=2),encoding='utf-8')
            print('Recovery: 3 checks passed; only recent still-active event reprocessed.')

if __name__=='__main__':asyncio.run(main(sys.argv[1]))
