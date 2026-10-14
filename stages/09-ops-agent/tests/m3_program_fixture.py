"""Explicit offline planner for actual local approval/broker/browser QA. No external model."""
import json
from ops_agent.config import Config
from ops_agent.main import create_app


def fixture():
    cfg=Config.load()
    if not cfg.database_url.endswith('/ma_m3_ops_test') or cfg.backend_url!='http://127.0.0.1:18103':raise ValueError('m3_isolated_fixture_required')
    cfg.model='fixture-m3-offline-planner'
    async def provider(messages,schemas):
        tool='assets__list' if len(messages)==2 else 'docker__restart_drill_container'
        args={} if tool=='assets__list' else {'asset_id':'ma-m3-drill'}
        return {'role':'assistant','content':None,'tool_calls':[{'id':'fixture-'+str(len(messages)),
            'function':{'name':tool,'arguments':json.dumps(args)}}]}
    return create_app(cfg,provider=provider)


app=fixture()
