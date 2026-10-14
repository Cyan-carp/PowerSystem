"""Explicit synthetic platform fixture in this M4 batch only; no notifications."""
import argparse,asyncio,json,time
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import urlsplit
import asyncpg
from redis.asyncio import Redis
ROOT=Path(__file__).resolve().parents[3]
async def main(a):
    run=(ROOT/a.run).resolve()
    if not run.is_relative_to(ROOT/'artifacts/stage10-security-agent') or not run.name.startswith('sec-m4-'):raise ValueError('dedicated_batch_required')
    env=dict(l.split('=',1) for l in (run/'private/local.env').read_text(encoding='utf-8').splitlines() if '=' in l)
    url=urlsplit(env['OPS_DATABASE_URL'])
    if (url.hostname,url.port,url.path)!=('127.0.0.1',15435,'/sec_m4_acceptance_test') or env['POSTGRES_DB']!='ma_m3_business_test' or env['REDIS_ADDR']!='127.0.0.1:16380':raise ValueError('dedicated_business_fixture_required')
    db=await asyncpg.connect(env['OPS_DATABASE_URL'].replace('/sec_m4_acceptance_test','/ma_m3_business_test'))
    rd=Redis(host='127.0.0.1',port=16380,db=0,password=env['REDIS_PASSWORD'])
    stamp=datetime.now(timezone.utc);ts=int(stamp.timestamp()*1000);items=[]
    try:
        devices=await db.fetch("SELECT id,device_code FROM devices WHERE is_simulated=true AND deleted_at IS NULL AND device_code IN ('INV-1001','INV-1002','INV-1003') ORDER BY id")
        if len(devices)!=3:raise ValueError('expected_synthetic_devices_required')
        for row in devices:
            latest={'device_id':row['id'],'device_code':row['device_code'],'run_id':run.name,'seq':1,'ts_ms':ts,'received_at':stamp.isoformat(),'voltage':400.0,'current':100.0,'temperature':45.0,'power':40.0,'status':1,'fault_code':0,'source':'synthetic-m4-fixture'}
            await rd.set('device:'+str(row['id'])+':latest',json.dumps(latest))
            await db.execute("INSERT INTO prediction_records(device_id,window_end_ms,probability,threshold,risk_level,model_version,top_factors,source) VALUES($1,$2,0.1,0.8,'low','sec-m4-synthetic-fixture','[]'::jsonb,'synthetic-fixture')",row['id'],ts)
            items.append({'device_id':row['id'],'code':row['device_code'],'synthetic':True})
    finally:await rd.aclose();await db.close()
    output=run/('synthetic-platform-'+str(ts)+'.json')
    output.write_text(json.dumps({'batch':run.name,'fixtures':items,'notifications':0,'mqtt':False},ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'synthetic_devices':len(items),'production_touched':False,'notifications':0}))
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--run',required=True);asyncio.run(main(p.parse_args()))
