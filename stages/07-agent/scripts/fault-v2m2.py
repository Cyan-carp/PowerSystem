"""Local-only Go outage / Redis stream / expired lease acceptance."""
import argparse
import asyncio
import importlib.util
import json
from pathlib import Path
from datetime import datetime
import httpx
from redis.asyncio import Redis
from agent.config import Config

spec=importlib.util.spec_from_file_location('smoke',Path(__file__).with_name('smoke-v2m2.py'))
smoke=importlib.util.module_from_spec(spec);spec.loader.exec_module(smoke)
state=Path('artifacts/stage7/v2m2/fault-20261001/state.json')

async def run(mode):
    cfg=Config.load();state.parent.mkdir(parents=True,exist_ok=True)
    redis=Redis.from_url(cfg.redis_url,decode_responses=True)
    async with httpx.AsyncClient(timeout=10,trust_env=False) as client:
        if mode=='deposit':
            identifier=smoke.alarm(0)
            smoke.sql(f"INSERT INTO agent_interpretations(event_key,category,alarm_id,level,occurred_at,task_status,attempts,lease_until,lease_token) SELECT 'alarm:'||id,'device_alarm',id,level,triggered_at,'running',1,now()-interval '1 second','expired-test' FROM alarm_records WHERE id={identifier}")
            batch=smoke.monitoring('go-outage',identifier)
            for _ in range(2):
                response=await client.post('http://127.0.0.1:8092/internal/agent/monitor-events',headers={'Authorization':'Bearer '+cfg.monitor_token},json=batch)
                assert response.status_code==200
            assert await redis.xlen('stage7:monitor:events')>=2
            key='monitor:'+batch['alerts'][0]['fingerprint']+':'+datetime.fromisoformat(batch['alerts'][0]['startsAt']).strftime('%Y-%m-%dT%H:%M:%S.%f').rstrip('0')+'Z'
            assert smoke.sql("SELECT count(*) FROM agent_monitor_events WHERE event_key='"+key+"'")=='0'
            state.write_text(json.dumps({'alarm_id':identifier,'monitor_key':key,'buffered':True}),encoding='utf-8')
            print('Go unavailable: independent alarm committed; duplicate monitor requests durably buffered before consumer restart.')
        else:
            data=json.loads(state.read_text())
            keys=['alarm:'+str(data['alarm_id']),data['monitor_key']]
            for _ in range(90):
                raw=smoke.sql("SELECT json_agg(json_build_object('event_key',event_key,'status',task_status,'attempts',attempts,'evidence',evidence,'result',result)) FROM agent_interpretations WHERE event_key IN ("+','.join("'"+key+"'" for key in keys)+")")
                rows=json.loads(raw) if raw else []
                if len(rows)==2 and all(row['status'] in ('completed','degraded') for row in rows):break
                await asyncio.sleep(1)
            assert len(rows)==2 and all(row['status']=='completed' for row in rows)
            assert next(row for row in rows if row['event_key']==keys[0])['attempts']==2
            assert smoke.sql("SELECT count(*) FROM agent_monitor_events WHERE event_key='"+keys[1]+"'")=='1'
            assert await redis.xlen('stage7:monitor:events')==0
            assert (await client.get('http://127.0.0.1:8080/api/v1/ping')).status_code==200
            state.with_name('results.json').write_text(json.dumps({'checks':{'restart_resume':True,'expired_lease':True,'monitor_dedup':True,'ack_after_commit':True,'stream_drained':True},'rows':rows},ensure_ascii=False,indent=2),encoding='utf-8')
            smoke.sql(f"UPDATE alarm_records SET status='recovered',recovered_at=now() WHERE id={data['alarm_id']}; UPDATE devices SET deleted_at=now() WHERE id=(SELECT device_id FROM alarm_records WHERE id={data['alarm_id']}); UPDATE agent_monitor_events SET recovered_at=now() WHERE event_key='{keys[1]}';")
            print('Go restarted: 5 fault-isolation checks passed; both real interpretations completed.')
    await redis.aclose()

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('mode',choices=['deposit','verify'])
    asyncio.run(run(parser.parse_args().mode))
