"""Leased workers with durable graph checkpoints and external-step journals."""
import asyncio
import base64
import hashlib
import json
import os
import time
import asyncpg
from datetime import datetime, timezone
from uuid import uuid4, uuid5, NAMESPACE_URL
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag
from fastapi import HTTPException
from .engine import Engine
from .ops import Ops, SupervisorTools
from .ma_security import digest, sign, claims
from .run_checkpoint import RunCheckpointer
from .run_store import LostLease, NeedsReconciliation, AwaitingAuthority, TERMINAL, encode, unpack


class RunCancelled(Exception):pass
class StepUncertain(Exception):pass


def run_profile(cfg):
    value={**cfg.provider_profile(),'endpoint_sha256':hashlib.sha256(cfg.llm_url.encode()).hexdigest(),'read_delegation_protocol':getattr(cfg,'v4_read_protocol','ma-run/v1')}
    from .production_profile import selected,PROFILE
    if selected():value['production_profile']=PROFILE
    return value


class AuthVault:
    def __init__(self,key):self.cipher=AESGCM(hashlib.sha256(('assistant-run-auth/v1:'+key).encode()).digest())
    def seal(self,user,value):
        nonce=os.urandom(12)
        return nonce+self.cipher.encrypt(nonce,encode(value).encode(),str(user).encode())
    def open(self,user,value):return json.loads(self.cipher.decrypt(value[:12],value[12:],str(user).encode()))


class BoundOps(Ops):
    """Read-only S03 child tasks are born with both run and parent-turn identity."""
    def __init__(self,*args,row,runstore=None,worker=None,**kwargs):
        super().__init__(*args,**kwargs)
        self.row=row
        self.dispatch_key=None
        self.run_wait=max(0,(row['deadline']-datetime.now(timezone.utc)).total_seconds())
        self.runstore,self.worker=runstore,worker
        self.read_children={str(r['task_id']) for r in row.get('result_refs',[]) if r.get('task_id') and not r.get('historical')}

    async def dispatch(self,intent,target=None,key=None,parent=None,verification=False,operation='read_only',task_kind='ops'):
        if operation!='read_only':raise HTTPException(403,'s03_write_not_enabled')
        return await super().dispatch(intent,target,key=self.dispatch_key or key,parent=self.row['turn_id'],verification=verification,operation=operation,task_kind=task_kind)

    async def request(self,method,path,body=None,signed=None):
        from .legacy_run_read import PROTOCOL, envelope, check_report
        legacy=getattr(self.cfg,'v4_read_protocol','ma-run/v1')==PROTOCOL
        expected_task=None
        if method=='POST' and path in ('/internal/ma/v1/tasks','/internal/ma/v1/verifications'):
            if legacy:
                body=envelope(self.row,self.dispatch_key,body,path.rsplit('/',1)[-1]);expected_task=body['task_id']
                self.read_children.add(expected_task)
                if self.runstore:await self.runstore.child(self.row['id'],self.worker,self.row['lease_epoch'],body['task_id'],'dispatching')
                # Keep the existing ma/v1 endpoint and signature unchanged.
                value=await super().request(method,path,body,body)
                return check_report(self.row,value,expected_task)
            body={**body,'run_id':str(self.row['id']),'protocol':'ma-run/v1'}
            body['task_id']=str(uuid5(self.row['id'],'task:'+str(self.dispatch_key)))
            if self.runstore:await self.runstore.child(self.row['id'],self.worker,self.row['lease_epoch'],body['task_id'],'dispatching')
            signed=body
            path='/internal/ma/v1/run-tasks'
        value=await super().request(method,path,body,signed)
        if legacy and method=='GET' and path.startswith('/internal/ma/v1/tasks/') and path.rsplit('/',1)[-1] in self.read_children:
            return check_report(self.row,value,path.rsplit('/',1)[-1])
        return value


class Runtime:
    def __init__(self,store,row,cfg,worker,ops):
        self.store,self.row,self.cfg,self.worker,self.ops=store,row,cfg,worker,ops
        self.checkpointer=RunCheckpointer(store,row['id'],worker,row['lease_epoch'],cfg.redact)

    def restore_tools(self,tools,result):
        if hasattr(tools,'egress_permits'):
            tools.egress_permits=result.get('_run_egress_permits',tools.egress_permits)
            tools.quality_bindings=result.get('_run_quality_bindings',tools.quality_bindings)

    async def check(self):
        row=await self.store.pool.fetchrow('SELECT cancel_requested,deadline,lease_owner,lease_epoch,lease_until>now() AS valid FROM assistant_runs WHERE id=$1',self.row['id'])
        if row['lease_owner']!=self.worker or row['lease_epoch']!=self.row['lease_epoch'] or not row['valid']:raise LostLease()
        if row['cancel_requested']:raise RunCancelled()
        if row['deadline']<=datetime.now(timezone.utc):raise TimeoutError()

    async def journal(self,key,kind,material,operation,result):
        await self.check()
        hashed=digest(material)
        async with self.store.pool.acquire() as c,c.transaction():
            await self.store.locked(c,self.row['id'],self.worker,self.row['lease_epoch'])
            step=await c.fetchrow('SELECT * FROM assistant_run_steps WHERE run_id=$1 AND step_key=$2 FOR UPDATE',self.row['id'],key)
            if step:
                if step['request_hash']!=hashed:raise StepUncertain('step_request_changed')
                if step['state']=='completed':
                    result.update(json.loads(step['result_state']))
                    return json.loads(step['output'])
                # Only read-only calls can reach this journal. Transport uncertainty gets one bounded retry.
                if step['attempts']>=2:raise StepUncertain('step_retry_exhausted')
                await c.execute('UPDATE assistant_run_steps SET attempts=attempts+1 WHERE run_id=$1 AND step_key=$2',self.row['id'],key)
            else:
                await c.execute("INSERT INTO assistant_run_steps(run_id,step_key,kind,request_hash,state) VALUES($1,$2,$3,$4,'started')",self.row['id'],key,kind,hashed)
        output=await operation()
        # A late completed read/model output is saved even if cancellation arrived during the call.
        safe_output=json.loads(self.cfg.redact(encode(output)))
        safe_state=json.loads(self.cfg.redact(encode(result)))
        async with self.store.pool.acquire() as c,c.transaction():
            await self.store.locked(c,self.row['id'],self.worker,self.row['lease_epoch'])
            await c.execute("UPDATE assistant_run_steps SET state='completed',output=$3::jsonb,result_state=$4::jsonb WHERE run_id=$1 AND step_key=$2",self.row['id'],key,encode(safe_output),encode(safe_state))
        return safe_output

    async def model(self,index,provider,messages,schemas,result):
        await self.store.emit(self.row['id'],self.worker,self.row['lease_epoch'],'progress',dict(phase='model',message='正在组织回答'))
        return await self.journal(f'model:{index}','model',dict(messages=messages,schemas=schemas),lambda:provider.complete(messages,schemas),result)

    async def tool(self,call_id,tools,name,args,jwt,result):
        if name in ('business_report','business_action','ops_action','schedule_task','rule_preview'):
            # All wrappers in the integrated candidate must observe the same
            # journalled call identity, even when another capability wraps them.
            layer=tools
            while layer is not None:
                layer.call_id=call_id
                layer=vars(layer).get('base')
        values=json.loads(args)
        if name=='dispatch_ops_task' and values.get('operation','read_only')!='read_only':
            return {'error':'S03不新增写动作能力'}
        key='tool:'+call_id
        self.ops.dispatch_key=uuid5(self.row['id'],key)
        await self.store.emit(self.row['id'],self.worker,self.row['lease_epoch'],'tool_started',dict(call_id=call_id,capability=name,status='running',summary='正在调用登记能力',result_ref=None,error=None))
        async def operation():
            output=await tools.execute(name,args,jwt,result)
            if name=='dispatch_ops_task' and isinstance(output,dict) and output.get('task_id'):
                while output.get('status') not in ('succeeded','failed','cancelled','timeout'):
                    await self.check();await asyncio.sleep(.2)
                    task=await self.ops.get(output['task_id'])
                    if task['status'] in ('succeeded','failed','cancelled','timeout'):
                        output=await tools.execute('get_ops_task',json.dumps({'task_id':task['task_id']}),jwt,result)
                        result['task_cards']=[{**t,'historical':False} for t in result.get('task_cards',[]) if t.get('task_id')!=task['task_id']]+[{**task,'historical':False}]
                        result['tool_calls'].extend(c for c in task.get('tool_calls',[]) if c not in result['tool_calls'])
                        result['sources'].extend(s for s in task.get('sources',[]) if s not in result['sources'])
                        tools.dispatched=task
                    else:output=task
            if hasattr(tools,'egress_permits'):
                result['_run_egress_permits']=tools.egress_permits
                result['_run_quality_bindings']={tid:{'business_quality':task.get('business_quality')} for tid,task in tools.quality_bindings.items()}
            return output
        output=await self.journal(key,'action_proposal' if name in ('business_action','ops_action','schedule_task') else 'read_tool',dict(name=name,args=values),operation,result)
        self.restore_tools(tools,result)
        await self.store.emit(self.row['id'],self.worker,self.row['lease_epoch'],'tool_finished',dict(call_id=call_id,capability=name,status='failed' if isinstance(output,dict) and output.get('error') else 'completed',summary='登记能力已返回',result_ref=output.get('task_id') if isinstance(output,dict) else None,error={'code':'tool_failed','message':'能力调用失败'} if isinstance(output,dict) and output.get('error') else None))
        return output


class RunWorker:
    def __init__(self,app,store,authority=None):
        self.app,self.store,self.authority=app,store,authority
        self.id=uuid4();self.tasks=set();self.stopping=False

    async def loop(self):
        while True:
            try:
                await self.drain_child_cancels()
                if (getattr(self.app.state.config,'v4_actions_enabled',False) or getattr(self.app.state.config,'v4_ops_enabled',False) or getattr(self.app.state.config,'v4_schedules_enabled',False)) and time.monotonic()-getattr(self,'last_action_sweep',0)>2:
                    self.last_action_sweep=time.monotonic()
                    for waiting in await self.store.pool.fetch("SELECT * FROM assistant_runs WHERE status='awaiting_approval' LIMIT 20"):
                        saved=unpack(waiting)
                        try:
                            state=await self.authority_state(saved,cancel=saved['cancel_requested'])
                            await self.store.project_authority(saved['id'],saved['user_id'],state.get('actions',[]))
                        except Exception:pass
                if getattr(self.app.state,'artifacts',None) and time.monotonic()-getattr(self,'last_artifact_sweep',0)>60:
                    await self.app.state.artifacts.expire()
                    self.last_artifact_sweep=time.monotonic()
                if len(self.tasks)<8:
                    row=await self.store.claim(self.id)
                    if row:
                        task=asyncio.create_task(self.execute(row));self.tasks.add(task)
                        task.add_done_callback(self.tasks.discard)
                        continue
            except (OSError,TimeoutError,asyncpg.PostgresError,asyncpg.InterfaceError):
                await asyncio.sleep(.5)
            await asyncio.sleep(.1)

    async def stop(self):
        self.stopping=True
        for task in list(self.tasks):task.cancel()
        await asyncio.gather(*list(self.tasks),return_exceptions=True)

    def bound_ops(self,row,auth):
        cfg=self.app.state.config
        actor=auth.get('actor')
        token=sign(claims(actor,row['session_id'],row['id'],hashlib.sha256(row['request']['message'].encode()).hexdigest()),cfg.ma_actor_key) if actor and cfg.ma_actor_key else ''
        return BoundOps(cfg,self.app.state.client,token,auth['jwt'],row['session_id'],row['user_id'],row['request']['message'],row=row)

    async def drain_child_cancels(self):
        record=await self.store.pool.fetchrow("UPDATE assistant_run_outbox SET cancel_retry_at=now()+interval '5 seconds' WHERE run_id=(SELECT run_id FROM assistant_run_outbox WHERE cancel_children AND cancel_retry_at<now() ORDER BY cancel_retry_at FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING run_id")
        if not record:return
        row=unpack(await self.store.pool.fetchrow('SELECT * FROM assistant_runs WHERE id=$1',record['run_id']))
        try:
            auth=AuthVault(self.app.state.config.service_token).open(row['user_id'],row['auth_sealed'])
            ops=self.bound_ops(row,auth)
            for ref in row['result_refs']:
                if ref.get('historical'):continue
                await ops.request('POST','/internal/ma/v1/tasks/'+str(ref['task_id'])+'/cancel',{})
            await self.store.pool.execute('UPDATE assistant_run_outbox SET cancel_children=false WHERE run_id=$1',row['id'])
            if row['status'] in TERMINAL:await self.store.pool.execute("UPDATE assistant_runs SET auth_sealed=''::bytea WHERE id=$1",row['id'])
        except (HTTPException,ValueError,KeyError,InvalidTag):pass

    async def authority_state(self,row,cancel=False,jwt=None):
        if self.authority:return await self.authority.inspect(row,cancel=cancel)
        if row['actions']:
            from .run_authority import Stage9Authority
            auth=dict(jwt=jwt,actor=row.get('auth_actor')) if jwt else AuthVault(self.app.state.config.service_token).open(row['user_id'],row['auth_sealed'])
            return await Stage9Authority(self.bound_ops(row,auth)).inspect(row,cancel=cancel)
        return dict(pending=False,actions=[])

    async def reconcile(self,row,result=None,messages=None):
        async def keep_lease():
            while True:
                await asyncio.sleep(3)
                if not await self.store.renew(row['id'],self.id,row['lease_epoch']):raise LostLease()
        heartbeat=asyncio.create_task(keep_lease())
        try:
            await self._reconcile(row,result,messages)
        finally:
            heartbeat.cancel();await asyncio.gather(heartbeat,return_exceptions=True)

    async def _reconcile(self,row,result=None,messages=None):
        if any(a.get('capability') in ('alarm.ack','device.remark','device.remark.read') or str(a.get('capability','')).startswith(('service.','schedule.')) or a.get('error_code') for a in row['actions']):
            return await self._business_reconcile(row)
        try:state=await self.authority_state(row,cancel=row['cancel_requested'])
        except Exception:state=dict(pending=True,actions=row['actions'])
        async with self.store.pool.acquire() as c,c.transaction():
            current=await self.store.locked(c,row['id'],self.id,row['lease_epoch'])
            current['actions']=state.get('actions',[])
            current['status']='reconciling';current['phase']='reconciling'
            await self.store._event(c,current,'progress',dict(phase='reconciling',message='正在核对既有动作的权威执行结果'))
            deadline=await c.fetchval("UPDATE assistant_runs SET reconciliation_deadline=coalesce(reconciliation_deadline,now()+interval '120 seconds') WHERE id=$1 RETURNING reconciliation_deadline",row['id'])
        while state.get('pending') and datetime.now(timezone.utc)<deadline:
            await asyncio.sleep(.2)
            if not await self.store.renew(row['id'],self.id,row['lease_epoch']):raise LostLease()
            try:
                row['cancel_requested']=await self.store.pool.fetchval('SELECT cancel_requested FROM assistant_runs WHERE id=$1',row['id'])
                state=await self.authority_state(row,cancel=row['cancel_requested'])
            except Exception:pass
        async with self.store.pool.acquire() as c,c.transaction():
            current=await self.store.locked(c,row['id'],self.id,row['lease_epoch'])
            current['actions']=state.get('actions',[])
            await self.store._event(c,current,'progress',dict(phase='reconciling',message='既有动作核查状态已记录'))
        effects=state.get('effects',False)
        if result is None:
            checkpoint=await RunCheckpointer(self.store,row['id'],self.id,row['lease_epoch']).aget_tuple(dict(configurable=dict(thread_id=str(row['id']))))
            result=(checkpoint.checkpoint.get('channel_values',{}).get('result_state',{}) if checkpoint else {}).copy()
        result.setdefault('answer_markdown',row['final_answer'] or '既有动作核查结果已保存；剩余计划已停止。')
        cancelled=await self.store.pool.fetchval('SELECT cancel_requested FROM assistant_runs WHERE id=$1',row['id'])
        status='partial' if state.get('pending') or effects or not cancelled and bool(result.get('answer_markdown')) else 'cancelled' if cancelled else 'failed'
        await self.store.finish(row['id'],self.id,row['lease_epoch'],status,result,messages or [],
            dict(code='requires_manual_reconciliation',message='权威结果尚待人工核查') if state.get('pending') else None,manual=bool(state.get('pending')))

    async def _business_reconcile(self,row):
        until=None
        while True:
            fresh=unpack(await self.store.pool.fetchrow('SELECT * FROM assistant_runs WHERE id=$1',row['id']))
            try:state=await self.authority_state(fresh,cancel=fresh['cancel_requested'])
            except Exception:state=dict(pending=True,actions=[{**a,'stale':True} for a in fresh['actions']])
            await self.store.project_authority(row['id'],row['user_id'],state.get('actions',[]))
            actions=state.get('actions',[])
            pending=any(a['status']=='pending' for a in actions)
            if pending and not fresh['cancel_requested']:
                async with self.store.pool.acquire() as c,c.transaction():
                    current=await self.store.locked(c,row['id'],self.id,row['lease_epoch'])
                    current.update(status='awaiting_approval',phase='awaiting_approval')
                    answer='动作尚未执行。请核对本轮目标、参数、版本、影响和有效期后批准或拒绝；批准只用于本次动作。'
                    if current['final_answer']!=answer:
                        current.update(final_answer=answer,answer_version=current['answer_version']+1)
                        await self.store._event(c,current,'text_delta',dict(answer_version=current['answer_version'],offset=0,text=answer,reset=True))
                    await c.execute('UPDATE rag_turns SET response=response||$2::jsonb WHERE id=$1',current['turn_id'],encode(dict(status='awaiting_approval',answer_markdown=answer,actions=current['actions'])))
                    await c.execute('UPDATE assistant_runs SET lease_until=NULL WHERE id=$1',row['id'])
                return
            uncertain=state.get('pending',True)
            if not uncertain:break
            if until is None:
                until=await self.store.pool.fetchval("UPDATE assistant_runs SET reconciliation_deadline=coalesce(reconciliation_deadline,now()+interval '120 seconds') WHERE id=$1 RETURNING reconciliation_deadline",row['id'])
            else:until=await self.store.pool.fetchval('SELECT reconciliation_deadline FROM assistant_runs WHERE id=$1',row['id'])
            if datetime.now(timezone.utc)>=until:break
            if not await self.store.renew(row['id'],self.id,row['lease_epoch']):raise LostLease()
            await asyncio.sleep(.5)
        cancelled=await self.store.pool.fetchval('SELECT cancel_requested FROM assistant_runs WHERE id=$1',row['id'])
        if uncertain:status='partial';answer='执行结果尚未确认，已停止后续计划，需人工对账；请勿重发操作。'
        elif all(a['status']=='succeeded' for a in actions):
            status='partial' if cancelled and state.get('effects') else 'succeeded'
            answer='\n\n'.join(a.get('effect_summary') or '动作结果已核实' for a in actions)
        elif all(a['status'] in ('rejected','cancelled','expired') for a in actions):status='cancelled';answer='动作已拒绝、取消或过期；未执行修改。'
        else:status='cancelled' if cancelled and all(a.get('no_effect') or a['status'] in ('rejected','cancelled','expired','invalidated','blocked') for a in actions) else 'partial' if state.get('effects') else 'failed';answer='本轮操作未全部完成。实际执行事实、健康检查与恢复边界见动作卡；请勿自动重发。'
        result=dict(answer_markdown=answer,status='answered' if status=='succeeded' else 'degraded',actions=actions,sources=[],tool_calls=[],warnings=[])
        if not cancelled and getattr(self.app.state,'artifacts',None) and any(a.get('capability')=='service.evidence' and a['status']=='succeeded' for a in actions):
            try:
                from .service_evidence import publish
                result['artifacts']=await publish(self.app.state.artifacts,row,self.id,actions)
                if result['artifacts']:result['answer_markdown']+='\n\n已生成本轮站内证据包，请在产物区下载；内容不发送模型。'
            except Exception:
                status='partial';result['answer_markdown']+='\n\n证据包生成未完成；已保留读取事实，请核查。'
        await self.store.finish(row['id'],self.id,row['lease_epoch'],status,result,[dict(role='assistant',content='本轮动作事实保存在原run，历史动作不自动重发。')],dict(code='requires_manual_reconciliation',message='执行结果尚待人工核查') if uncertain else None,manual=uncertain)

    async def execute(self,row):
        cfg=self.app.state.config
        result=dict(request_id=str(row['id']),session_id=str(row['session_id']),turn_id=row['turn_id'],status='degraded',answer_markdown='',sources=[],tool_calls=[],warnings=[],model=cfg.model,response_mode='model',model_called=False)
        messages=[];work=None;monitor=None
        async def interrupted(status,error=None):
            current=unpack(await self.store.pool.fetchrow('SELECT * FROM assistant_runs WHERE id=$1',row['id']))
            if any(not ref.get('historical') for ref in current['result_refs']):
                await self.store.enqueue_child_cancel(row['id'])
            if current['actions']:
                await self.reconcile(current,result,messages)
            else:
                await self.store.finish(row['id'],self.id,row['lease_epoch'],status,result,messages,error)
        async def watch(runtime):
            while True:
                await asyncio.sleep(.1)
                await runtime.check()
                if not await self.store.renew(row['id'],self.id,row['lease_epoch']):raise LostLease()
        try:
            if row['status']=='reconciling' or row['actions']:
                await self.reconcile(row);return
            if digest(row['model_profile'])!=digest(run_profile(cfg)):
                await self.store.finish(row['id'],self.id,row['lease_epoch'],'failed',result,[],dict(code='model_configuration_changed',message='受理时绑定的模型配置已变化，未向新目的地重放'))
                return
            auth=AuthVault(cfg.service_token).open(row['user_id'],row['auth_sealed'])
            ops=self.bound_ops(row,auth)
            ops.runstore=self.store;ops.worker=self.id
            runtime=Runtime(self.store,row,cfg,self.id,ops)
            await runtime.check()
            tools=SupervisorTools(self.app.state.tools,ops,time.monotonic()+max(0,(row['deadline']-datetime.now(timezone.utc)).total_seconds()))
            if self.app.state.fixture_tools and hasattr(self.app.state.tools,'run_tools'):
                tools=self.app.state.tools.run_tools(ops)
            if getattr(cfg,'v4_artifacts_enabled',False) and getattr(self.app.state,'artifacts',None):
                from .artifacts import ReportTools
                tools=ReportTools(tools,self.app.state.artifacts,runtime)
            if getattr(cfg,'v4_actions_enabled',False):
                from .actions import ActionTools
                tools=ActionTools(tools,runtime,self.app.state.tools)
            if getattr(cfg,'v4_ops_enabled',False):
                from .service_ops import ServiceTools
                tools=ServiceTools(tools,runtime)
            if getattr(cfg,'v4_schedules_enabled',False):
                from .schedule_tools import ScheduleTools
                tools=ScheduleTools(tools,runtime,self.app.state.schedules)
            monitor=asyncio.create_task(watch(runtime))
            request=await self.store.pool.fetchrow('SELECT * FROM assistant_schedule_requests WHERE run_id=$1',row['id'])
            slot=await self.store.pool.fetchrow('SELECT * FROM assistant_schedule_slots WHERE run_id=$1',row['id'])
            if request or slot:
                work=asyncio.create_task(self.app.state.schedules.run(runtime,auth,result,request,slot))
            else:work=asyncio.create_task(Engine(cfg,self.app.state.provider,tools).run(row['request']['message'],row['context_snapshot']['turns'],auth['jwt'],result,runtime=runtime))
            done,_=await asyncio.wait((work,monitor),return_when=asyncio.FIRST_COMPLETED)
            if monitor in done:await monitor
            messages=await work
            await runtime.check()
            if result.get('_run_action_pause'):
                current=unpack(await self.store.pool.fetchrow('SELECT * FROM assistant_runs WHERE id=$1',row['id']))
                await self._business_reconcile(current);return
            pending=any(t.get('status') not in ('succeeded','failed','cancelled','timeout') for t in result.get('task_cards',[]))
            # A returned acceptance receipt is never a successful run completion.
            if pending:
                raise StepUncertain('child_task_incomplete')
            status='succeeded' if result['status']=='answered' else 'failed'
            error=result.get('error') if status=='failed' else None
            if status=='succeeded' and result.get('objective_quality',{}).get('complete') is False:
                status='partial' if result.get('task_cards') else 'failed'
                error=dict(code='objective_incomplete',message='本轮问题目标尚未完成，已保留实际取得的结果')
            await self.store.finish(row['id'],self.id,row['lease_epoch'],status,json.loads(cfg.redact(encode(result),(auth['jwt'],))),messages,error)
        except RunCancelled:
            # Read-only stage9 children are cancelled using their original owner context.
            try:
                if 'ops' in locals():
                    for card in result.get('task_cards',[]):
                        if card.get('historical'):continue
                        await ops.request('POST','/internal/ma/v1/tasks/'+str(card['task_id'])+'/cancel',{})
            except HTTPException:pass
            await interrupted('cancelled')
        except TimeoutError:
            await interrupted('failed',dict(code='deadline_exceeded',message='运行预算已耗尽'))
        except NeedsReconciliation:
            await interrupted('failed')
        except AwaitingAuthority:
            # Existing controller projection owns the pause; keep the completed graph checkpoint.
            await self.store.pool.execute('UPDATE assistant_runs SET lease_until=NULL WHERE id=$1 AND lease_owner=$2 AND lease_epoch=$3',row['id'],self.id,row['lease_epoch'])
        except LostLease:pass
        except asyncio.CancelledError:raise
        except Exception:
            if 'slot' in locals() and slot:
                # Fence a failed scheduled read before a restored permission
                # could reuse its old grant. Release any publication transaction
                # before acquiring the schedule's reduction lock.
                if work and not work.done():
                    work.cancel();await asyncio.gather(work,return_exceptions=True)
                await self.app.state.schedules.reduce(slot['schedule_id'],row['user_id'],reason='scheduled_read_failed')
            await interrupted('failed',dict(code='run_failed',message='运行未完成；已保存步骤可供核对'))
        finally:
            for task in (work,monitor):
                if task and not task.done():task.cancel()
            await asyncio.gather(*(t for t in (work,monitor) if t),return_exceptions=True)
            if 'slot' in locals() and slot:
                await self.store.pool.execute("UPDATE assistant_schedule_slots t SET status=r.status FROM assistant_runs r WHERE t.run_id=r.id AND r.id=$1 AND r.status IN ('succeeded','partial','failed','cancelled')",row['id'])
