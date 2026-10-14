"""Controller boundary unit tests. No claim of PostgreSQL transaction verification."""
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from ops_agent.reviews import ReviewController, ControlFailure
from review_contract import digest


class SavedProgramState:
    def __init__(self):
        self.task={'task_id':uuid4(),'trace_id':uuid4(),'user_id':101,'status':'planning','execution_epoch':1,
                   'deadline':datetime.now(timezone.utc)+timedelta(seconds=200),
                   'envelope':json.dumps({'intent':'SEC-M1 synthetic:审查合成摘要','actor':{'user_id':101,'role':'operator'}})}
        args={'asset_id':'synthetic-host'}
        self.step={'tool':'linux.get_cpu_usage','version':'1.0','risk_class':'R0','execution_epoch':1,
                   'args':json.dumps(args),'args_hash':digest(args),'asset_id':None,'asset_version':None,
                   'result':json.dumps({'security_context':{'evidence':[]}})}
    async def fetchrow(self,query,*args):
        if 'FROM ma_tasks ' in query:return self.task if args[0]==self.task['task_id'] and args[1]==101 else None
        if 'FROM ma_task_steps ' in query:return self.step if args[1]==1 else None
        raise AssertionError('unexpected_boundary_query')
    async def fetchval(self,query):
        assert query=='SELECT current_database()'
        return 'sec_m1_regression_test'


@pytest.mark.parametrize('mutation',['owner','role','step','epoch','hash','tool','version','risk','args','raw','production','cancelled'])
async def test_SEC_C03_program_boundary_before_model(mutation):
    pool=SavedProgramState();controller=ReviewController(pool,None,'S'*40,'Q'*40,'R'*40)
    params={'task_id':pool.task['task_id'],'step_no':1,'user_id':101,'role':'operator','review_id':uuid4(),'kind':'sensitive_read'}
    if mutation=='owner':params['user_id']=999
    if mutation=='role':params['role']='admin'
    if mutation=='step':params['step_no']=2
    if mutation=='epoch':pool.step['execution_epoch']=2
    if mutation=='hash':pool.step['args_hash']='a'*64
    if mutation=='tool':pool.step['tool']='shell.execute'
    if mutation=='version':pool.step['version']='99'
    if mutation=='risk':pool.step['risk_class']='R2'
    if mutation=='args':
        pool.step['args']=json.dumps({'asset_id':'synthetic-host','command':'reboot'})
        pool.step['args_hash']=digest(json.loads(pool.step['args']))
    if mutation=='raw':pool.step['result']=json.dumps({'security_context':{'evidence':[],'raw_log':'unlicensed'}})
    if mutation=='production':pool.task['envelope']=json.dumps({'intent':'真实任务','actor':{'user_id':101,'role':'operator'}})
    if mutation=='cancelled':pool.task['status']='cancelled'
    with pytest.raises((ControlFailure,ValueError)):await controller.build(**params)


async def test_controller_derives_request_from_saved_state():
    pool=SavedProgramState();controller=ReviewController(pool,None,'S'*40,'Q'*40,'R'*40)
    request=await controller.build(pool.task['task_id'],1,101,'operator',uuid4(),'inspection_report')
    assert request.trace_id==pool.task['trace_id'] and request.tool.args_hash==pool.step['args_hash']
    assert request.deadline==pool.task['deadline'] and request.normalized_intent=='审查合成摘要'


@pytest.mark.parametrize('name',['ma_ops','powersystem','ma_m3_ops_regression_test'])
async def test_SEC_C03_controller_rejects_non_sec_database(name):
    pool=SavedProgramState()
    async def fetchval(query):return name
    pool.fetchval=fetchval
    controller=ReviewController(pool,None,'S'*40,'Q'*40,'R'*40)
    with pytest.raises(ControlFailure):await controller.build(pool.task['task_id'],1,101,'operator',uuid4(),'sensitive_read')


@pytest.mark.parametrize('enabled,model,timeout,allowed',[
    (False,'gpt-5.6-sol',120,False),(True,'gpt-5.6-sol',120,True),
    (True,'gpt-5.6-sol',121,False),(True,'deepseek-v4-flash-0731',120,False),
    ('true','gpt-5.6-sol',120,False)])
def test_controller_timeout_exception_guard(enabled,model,timeout,allowed):
    def create():return ReviewController(None,None,'S'*40,'Q'*40,'R'*40,model=model,timeout=timeout,
                                        acceptance_timeout_exception=enabled)
    if allowed:create()
    else:
        with pytest.raises(ValueError):create()
