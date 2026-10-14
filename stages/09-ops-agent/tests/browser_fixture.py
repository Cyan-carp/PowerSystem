"""Explicit loopback-only delayed planner fixture for deterministic browser polling QA.
Never configure this module as a runtime entry; its health reports a fixture model.
Platform GET and authentication still use the actual local services.
"""
import asyncio
from ops_agent.main import create_app
from ops_agent.config import Config


def create_fixture():
    cfg=Config.load()
    if not cfg.database_url.split('?')[0].endswith('_test') or not cfg.backend_url.startswith('http://127.0.0.1:'):
        raise ValueError('dedicated_loopback_test_required')
    cfg.model='fixture-delayed-planner'
    async def provider(messages,schemas):
        if len(messages)==2:
            await asyncio.sleep(30)
            return {'role':'assistant','content':None,'tool_calls':[{'id':'fixture-read','function':{'name':'platform__get_dashboard_summary','arguments':'{}'}}]}
        await asyncio.sleep(5)
        return {'role':'assistant','content':'{"summary":"页面延迟fixture；实际平台总览GET已保存。","hypotheses":[],"recommendations":[]}'}
    return create_app(cfg,provider=provider)
