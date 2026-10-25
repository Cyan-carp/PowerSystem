import asyncio,json,time,hashlib
from pathlib import Path
from uuid import uuid4
import httpx
OUT=Path.cwd()/'artifacts/v4-assistant/s04-20261023-r1'
BASE='http://127.0.0.1:28942/api/v1';DRILL='http://127.0.0.1:28941/drill'
report={'environment':'real loopback Go JWT + FastAPI Store.open + PostgreSQL; synthetic business/provider unless marked official','checks':{},'receipts':{},'failures':[],'timeline':[],'official_cost':{'amount':None,'reason':'account billing not inspected'}}
async def main():
 async with httpx.AsyncClient(trust_env=False,timeout=30) as c:
  async def auth(user):
   r=await c.post(BASE+'/auth/login',json={'username':user});r.raise_for_status();return {'Authorization':'Bearer '+r.json()['data']['token']}
  h=await auth('s04-local');other=await auth('s04-other')
  async def get(rid,headers=h):
   r=await c.get(BASE+'/agent/runs/'+rid,headers=headers);r.raise_for_status();return r.json()
  async def submit(message,sid=None,key=None):
   key=key or str(uuid4());r=await c.post(BASE+'/agent/runs',json={'message':message,**({'session_id':sid} if sid else {})},headers={**h,'Idempotency-Key':key});r.raise_for_status();assert r.status_code==202
   return r.json(),key
  async def done(rid):
   for _ in range(650):
    snap=await get(rid)
    if snap['status'] in ('succeeded','partial','failed','cancelled'):return snap
    await asyncio.sleep(.1)
   raise TimeoutError(rid)
  seed_response=await c.post(DRILL+'/seed');seed_response.raise_for_status();seed=seed_response.json();report['legacy']=seed
  a,_=await submit('HTTP目标甲 慢');sid=a['session_id'];b,_=await submit('HTTP目标乙 中速',sid);d,_=await submit('HTTP目标丙 快',sid)
  report['receipts']['three']=[a,b,d]
  pending={x['run_id']:x for x in (a,b,d)};finished=[]
  while pending:
   for rid,receipt in list(pending.items()):
    snap=await get(rid);report['timeline'].append({'run_id':rid,'status':snap['status'],'seq':snap['last_seq']})
    assert snap['turn_id']==receipt['turn_id']
    if snap['status'] in ('succeeded','failed','cancelled','partial'):
     assert snap['status']=='succeeded';finished.append(rid);pending.pop(rid)
   await asyncio.sleep(.1)
  assert finished.index(a['run_id'])>finished.index(b['run_id'])
  report['checks']['three_targets_out_of_order_original_turn']=True
  report['checks']['queue_and_running_observed']={'queued','running'}<=set(x['status'] for x in report['timeline'])
  r=await c.get(BASE+'/agent/runs/'+a['run_id'],headers=other);assert r.status_code==404
  r=await c.get(BASE+'/agent/runs/'+a['run_id']+'/events',headers=other);assert r.status_code==404
  r=await c.post(BASE+'/agent/runs/'+a['run_id']+'/cancel',headers=other);assert r.status_code==404
  report['checks']['cross_user_snapshot_stream_cancel_denied']=True
  r=await c.get(BASE+'/agent/runs/'+a['run_id']+'/events',headers={**h,'Last-Event-ID':'2'},params={'after_seq':1});assert r.status_code==400
  snap=await get(a['run_id']);r=await c.get(BASE+'/agent/runs/'+a['run_id']+'/events',headers=h,params={'after_seq':snap['last_seq']+1});assert r.status_code==400
  report['checks']['conflicting_and_future_cursor_rejected']=True
  before=await get(a['run_id']);await c.post(DRILL+'/'+a['run_id']+'/control',json={'op':'expire'})
  r=await c.get(BASE+'/agent/runs/'+a['run_id']+'/events',headers=h,params={'after_seq':0});assert r.status_code==410
  after=await get(a['run_id']);assert before['final_answer']==after['final_answer']
  report['checks']['real_410_preserves_single_run_snapshot']=True
  item,key=await submit('HTTP丢失回执演练',sid)
  retry,_=await submit('HTTP丢失回执演练',sid,key);assert item==retry
  report['checks']['same_key_same_run']=True
  r=await c.post(BASE+'/agent/runs',json={'message':'不同请求','session_id':sid},headers={**h,'Idempotency-Key':key});assert r.status_code==409
  report['checks']['changed_request_same_key_conflict']=True
  failed,_=await submit('失败场景',sid);assert (await done(failed['run_id']))['status']=='failed'
  budget,_=await submit('预算场景',sid);await c.post(DRILL+'/'+budget['run_id']+'/control',json={'op':'budget'});assert (await done(budget['run_id']))['error']['code']=='deadline_exceeded'
  cancel,_=await submit('取消运行场景',sid);r=await c.post(BASE+'/agent/runs/'+cancel['run_id']+'/cancel',headers=h);r.raise_for_status();cancelled=await done(cancel['run_id']);assert cancelled['status']=='cancelled' and cancelled['cancel_requested']
  report['checks']['failure_budget_and_actual_cancel']=True
  tool,_=await submit('工具查询 慢',sid);report['receipts']['tool']=tool
  async with c.stream('GET',BASE+'/agent/runs/'+tool['run_id']+'/events',headers=h) as stream:
   async for line in stream.aiter_lines():
    if line.startswith('data: ') and json.loads(line[6:])['type']=='tool_started':break
  # Real TCP subscriber goes away; no cancellation was issued.
  current=await get(tool['run_id']);assert not current['cancel_requested']
  result=await done(tool['run_id']);assert result['status']=='succeeded'
  r=await c.get(BASE+'/agent/runs/'+tool['run_id']+'/events',headers=h);events=[json.loads(line[6:]) for line in r.text.splitlines() if line.startswith('data: ')]
  assert any(e['type']=='tool_finished' and e['data']['status']=='completed' for e in events)
  report['checks']['tool_disconnect_http_resume_no_cancel']=True
  official,_=await submit('官方:这是隔离合成浏览器验收，请用一句中文说明断线后任务可继续。不查询任何数据。',sid)
  result=await done(official['run_id']);assert result['status']=='succeeded' and result['final_answer']
  report['official']={'receipt':official,'snapshot':result,'candidate':{'endpoint':'https://api.deepseek.com','model':'deepseek-flash','thinking':'disabled','credential_reference':'S03 candidate-registry stage8_assistant'},'scope':'one synthetic natural-language integration sample; inherits S02/S03 quality/latency, no new full model benchmark'}
  report['checks']['official_candidate_real_http_completion']=True
  report['receipts']['session']=sid
  report['receipts']['failure']=failed;report['receipts']['budget']=budget;report['receipts']['cancel']=cancel
  legacy=(await c.get(BASE+'/agent/sessions/'+seed['session_id'],headers=h)).json()['data']['turns'][0]
  assert legacy.get('run_id') is None and legacy['response']['answer_markdown']=='旧历史原文：S04-LEGACY-UNCHANGED'
  report['checks']['legacy_nullable_run_and_content_preserved']=True
try:asyncio.run(main())
except Exception as e:report['failures'].append(type(e).__name__+': '+str(e));raise
finally:(OUT/'http-acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8');print(json.dumps({'checks':report['checks'],'failures':report['failures']},ensure_ascii=False))
