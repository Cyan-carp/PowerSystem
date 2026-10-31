"""Bounded S05 integration verification; saves each failed/intermediate batch."""
import asyncio, csv, hashlib, io, json, sys, time, traceback, zipfile
from datetime import datetime,timezone
from pathlib import Path
from uuid import uuid4,UUID
import xml.etree.ElementTree as ET
import asyncpg,httpx
ROOT=Path(__file__).resolve().parents[3]
OUT=ROOT/'artifacts/v4-智能助手/S05-业务分析-20261023-r1'
DSN='postgresql://s05_test@127.0.0.1:25435/v4_s05_20261023_r1_test'
BASE='http://127.0.0.1:28952'


def workbook(raw):
    ns={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        assert z.testzip() is None
        sheet=ET.fromstring(z.read('xl/worksheets/sheet1.xml'))
        assert not sheet.findall('.//s:f',ns)
        return [[c.find('s:is/s:t',ns).text or '' for c in r] for r in sheet.findall('s:sheetData/s:row',ns)]


async def main():
    batch=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    report=dict(batch=batch,checks={},runs=[],files=[],errors=[],browser='not run: Computer Use stopped because current browser URL could not be determined reliably',skipped=['Previously signed unaffected S04 browser cases and full historical suites per owner instruction'],cost=dict(amount=None,reason='billing not inspected'))
    def check(name,condition):
        report['checks'][name]=bool(condition)
        if not condition:raise AssertionError(name)
    conn=await asyncpg.connect(DSN)
    async with httpx.AsyncClient(timeout=150,trust_env=False) as client:
        user=(await client.post(BASE+'/api/v1/auth/login',json={'username':'s05-owner'})).json()['data']
        other=(await client.post(BASE+'/api/v1/auth/login',json={'username':'s05-other'})).json()['data']
        h={'Authorization':'Bearer '+user['token']};oh={'Authorization':'Bearer '+other['token']}
        async def get(path,headers=h):return await client.get(BASE+path,headers=headers)
        async def submit(question,sid=None,key=None):
            key=key or str(uuid4());body=dict(message=question)
            if sid:body['session_id']=sid
            response=await client.post(BASE+'/api/v1/agent/runs',headers={**h,'Idempotency-Key':key},json=body)
            assert response.status_code==202,response.text
            receipt=response.json();report['runs'].append(dict(question=question,receipt=receipt,key=key))
            return receipt,key
        async def wait(receipt):
            for _ in range(650):
                r=await get('/api/v1/agent/runs/'+receipt['run_id']);assert r.status_code==200
                data=r.json();assert data['turn_id']==receipt['turn_id'] and data['session_id']==receipt['session_id']
                if data['status'] in ('succeeded','partial','failed','cancelled'):return data
                await asyncio.sleep(.15)
            raise TimeoutError(receipt['run_id'])
        async def files(snapshot):
            assert len(snapshot['artifacts'])==2,snapshot
            saved={}
            for item in snapshot['artifacts']:
                r=await get('/api/v1/agent/artifacts/'+item['artifact_id']+'/download')
                assert r.status_code==200,r.text
                assert hashlib.sha256(r.content).hexdigest()==item['sha256']
                dest=OUT/'downloads'/batch/(snapshot['run_id']+'-'+item['name']);dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(r.content)
                report['files'].append(dict(path=str(dest.relative_to(OUT)),sha256=item['sha256'],size=len(r.content),artifact_id=item['artifact_id']))
                saved[item['name'].rsplit('.',1)[1]]=r.content
            rows=list(csv.reader(io.StringIO(saved['csv'].decode('utf-8-sig'),newline='')))
            check('csv_xlsx_'+snapshot['run_id'],workbook(saved['xlsx'])==rows)
            return rows
        try:
            slow,key=await submit('验收:energy station 慢')
            fast,_=await submit('验收:energy device',slow['session_id'])
            # Stop one real event subscription early; the worker remains alive.
            async with client.stream('GET',BASE+'/api/v1/agent/runs/'+slow['run_id']+'/events',headers=h) as stream:
                async for line in stream.aiter_lines():
                    if line.startswith('data:'): break
            fs=await wait(fast);ss=await wait(slow)
            check('out_of_order_original_turn',fs['terminal_event']['at']<ss['terminal_event']['at'] and fs['turn_id']!=ss['turn_id'])
            check('stop_subscription_does_not_cancel',ss['status']=='succeeded' and not ss['cancel_requested'])
            slowrows=await files(ss);fastrows=await files(fs)
            check('energy_totals_and_missing',slowrows[1][5]=='22.5' and sum(r[0]=='missing' for r in slowrows)==3 and any(r[0]=='group_total' and r[2]=='PV-02' and r[5]=='' for r in slowrows))
            check('energy_station_device_consistent',fastrows[1][5]==slowrows[1][5] and sum(r[0]=='group_total' for r in fastrows)==3)
            repeat,_=await submit('验收:energy station 慢',None,key)
            check('same_key_same_run',repeat['run_id']==slow['run_id'])
            conflict=await client.post(BASE+'/api/v1/agent/runs',headers={**h,'Idempotency-Key':key},json={'message':'different'})
            check('changed_key_request_conflict',conflict.status_code==409)
            # Re-read persisted original turn and real snapshot after reconnect.
            history=(await get('/api/v1/agent/sessions/'+slow['session_id'])).json()['data']['turns']
            check('history_original_binding',all(next(t for t in history if t['id']==r['turn_id'])['run_id']==r['run_id'] for r in (slow,fast)))
            check('snapshot_artifacts_recover', (await get('/api/v1/agent/runs/'+slow['run_id'])).json()['artifacts']==ss['artifacts'])
            # Verify empty and intersection behavior at the authoritative backend.
            e=(await get('/api/v1/reports/energy?start_date=2026-10-20&end_date=2026-10-22&scope=business&station_code=ST-01')).json()['data']
            check('energy_intersection_null',e['total_kwh'] is None and not e['complete'] and e['devices']==[])
            cases={}
            for kind in ('availability','alarm-timeliness','telemetry','investigation'):
                receipt,_=await submit('验收:'+kind,slow['session_id']);s=await wait(receipt)
                check(kind+'_complete_run',s['status']=='succeeded' and s['actions']==[])
                rows=await files(s);cases[kind]=s
                if kind in ('availability','alarm-timeliness'):
                    direct=await get('/api/v1/reports/'+kind+'?format=csv&start=2026-10-20T00:00:00Z&end=2026-10-21T00:00:00Z')
                    comparison=list(csv.reader(io.StringIO(direct.content.decode('utf-8-sig'))))
                    # Computed-at reflects two actual calls; every other column is identical.
                    ci=rows[0].index('计算时间')
                    check(kind+'_existing_csv_fields',[[x for i,x in enumerate(row) if i!=ci] for row in comparison]==[[x for i,x in enumerate(row) if i!=ci] for row in rows])
                if kind=='investigation':check('investigation_sources_gaps',len(rows)==3 and all(r[9].startswith('/api/v1/alarms/') and 'start=' in r[10] and r[8] for r in rows[1:]))
            for question in ('验收:energy 超限','验收:energy 空报表'):
                receipt,_=await submit(question);s=await wait(receipt)
                if '超限' in question:check('over_limit_no_fake_artifact',s['status']=='failed' and s['artifacts']==[])
                else:
                    rows=await files(s);check('empty_report_not_zero',rows[1][5]=='' and rows[1][6]=='false')
            aid=ss['artifacts'][0]['artifact_id']
            check('cross_user_download_denied',(await get('/api/v1/agent/artifacts/'+aid+'/download',oh)).status_code==404)
            check('cross_user_metadata_denied',(await get('/api/v1/agent/artifacts/'+aid,oh)).status_code==404)
            check('invalid_download',(await get('/api/v1/agent/artifacts/'+str(uuid4())+'/download')).status_code==404)
            check('bad_id_rejected',(await get('/api/v1/agent/artifacts/invalid/download')).status_code==400)
            expired=cases['availability']['artifacts'][1]['artifact_id']
            await conn.execute("UPDATE assistant_artifacts SET expires_at=now()-interval '1 second' WHERE id=$1",UUID(expired))
            check('expired_denied',(await get('/api/v1/agent/artifacts/'+expired+'/download')).status_code==410)
            damaged=cases['alarm-timeliness']['artifacts'][1]['artifact_id']
            damaged_key=await conn.fetchval('SELECT storage_key FROM assistant_artifacts WHERE id=$1',UUID(damaged))
            (OUT/'files'/damaged_key).write_bytes(b'damaged acceptance artifact')
            check('corrupt_denied',(await get('/api/v1/agent/artifacts/'+damaged+'/download')).status_code==410)
            await conn.execute("UPDATE users SET role='disabled' WHERE id=50001")
            try:check('current_identity_rechecked',(await get('/api/v1/agent/artifacts/'+aid+'/download')).status_code==401)
            finally:await conn.execute("UPDATE users SET role='operator' WHERE id=50001")
            audits=[dict(r) for r in await conn.fetch('SELECT artifact_id,user_id,outcome,at FROM assistant_artifact_downloads ORDER BY id')]
            report['audit']=json.loads(json.dumps(audits,default=str))
            check('download_audit',{'authorized_delivery','stream_served','not_found_or_not_owner','expired','integrity_failed'}<={x['outcome'] for x in audits})
            check('migration_real_startup',await conn.fetchval("SELECT to_regclass('assistant_artifacts') IS NOT NULL AND to_regclass('assistant_artifact_downloads') IS NOT NULL"))
            # Two real official samples, not a repeat of the inherited quality suite.
            for question in ('官方:请导出2026-10-20当天设备可用率报表，所有设备，返回CSV和XLSX。开始2026-10-20T00:00:00+08:00，结束2026-10-21T00:00:00+08:00。','官方:请按设备导出业务范围2026-10-20至2026-10-22（结束不含）的发电量汇总及缺口，CSV和XLSX。'):
                receipt,_=await submit(question);snapshot=await wait(receipt)
                check('official_'+receipt['run_id'],snapshot['status']=='succeeded' and len(snapshot['artifacts'])==2)
                await files(snapshot)
            report['verdict']='http_and_real_file_acceptance_passed_browser_pending'
        except Exception as e:
            report['errors'].append(dict(type=type(e).__name__,message=str(e)[:1200],traceback=traceback.format_exc()[-2500:]))
            report['verdict']='failed_batch'
        finally:
            await conn.close()
            (OUT/('http-'+batch+'.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(dict(batch=batch,verdict=report['verdict'],checks=len(report['checks']),errors=report['errors']),ensure_ascii=False))
    if report['errors']:sys.exit(1)

if __name__=='__main__':asyncio.run(main())
