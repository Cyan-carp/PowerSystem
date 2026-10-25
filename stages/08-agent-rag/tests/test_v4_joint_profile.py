from dataclasses import replace
import pytest
from rag_agent.config import Config
from rag_agent.joint_profile import validate

def candidate():
    return Config(model='deepseek-flash',llm_url='https://api.deepseek.com',llm_key='',service_token='x'*40,
        database_url='postgresql://test@127.0.0.1:25439/v4_s09_r2_joint_rag_test',redis_url='',
        backend_url='http://127.0.0.1:28992',ops_url='http://127.0.0.1:28973',
        v4_joint_profile='v4-s09-local',v4_assistant_enabled=True,v4_runs_enabled=True,
        v4_actions_enabled=True,v4_ops_enabled=True,v4_schedules_enabled=True)

@pytest.mark.parametrize('changes',[
    {'database_url':'postgresql://test@postgres:5432/powersystem_agent_rag'},
    {'database_url':'postgresql://test@127.0.0.1:25439/v4_s09_r2_joint_rag_test?database=production'},
    {'database_url':'postgresql://test@127.0.0.1:25439/v4_s09_r2_joint_ops_test'},
    {'database_url':'postgresql://test@127.0.0.1:25438/v4_s08_fixture_test'},
    {'v4_joint_profile':'v4-s06-local'}, {'v4_actions_enabled':False},
    {'ops_url':'http://127.0.0.1:28983'}, {'backend_url':'http://api:8080'},
])
def test_joint_does_not_upgrade_other_profiles(monkeypatch,changes):
    monkeypatch.setenv('V4_S09_LOCAL_ENABLED','true')
    with pytest.raises(ValueError):validate(replace(candidate(),**changes))

def test_joint_requires_explicit_domain(monkeypatch):
    monkeypatch.delenv('V4_S09_LOCAL_ENABLED',raising=False)
    with pytest.raises(ValueError):validate(candidate())
    monkeypatch.setenv('V4_S09_LOCAL_ENABLED','true')
    validate(candidate())

@pytest.mark.asyncio
async def test_composed_tools_keep_unique_journal_identity():
    # This exercises Runtime.tool before journal execution, catching loss of the
    # inner report/action identity when schedules wraps both capabilities.
    from rag_agent.run_worker import Runtime
    from types import SimpleNamespace
    from uuid import uuid4
    class Wrapper:
        def __init__(self,base=None):self.base=base;self.call_id=None
        async def execute(self,name,args,jwt,result):
            assert self.call_id=='call-2'
            if self.base:return await self.base.execute(name,args,jwt,result)
            return {'ok':True}
    chain=Wrapper(Wrapper(Wrapper()))
    class Store:
        async def emit(self,*args):pass
    class Harness:
        row={'id':uuid4(),'lease_epoch':1};worker=uuid4();store=Store()
        ops=SimpleNamespace()
        def restore_tools(self,*args):pass
        async def journal(self,key,kind,request,operation,result):return await operation()
    await Runtime.tool(Harness(),'call-2',chain,'business_action','{}','',{})
