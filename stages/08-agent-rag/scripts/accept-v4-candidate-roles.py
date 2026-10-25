"""Explicit synthetic-only candidate registry. Never edits runtime services."""
import argparse,asyncio,json,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import httpx
from rag_agent.config import Config
from rag_agent.provider import Provider

ROLES={'stage7_chat','stage7_interpretation','stage8_assistant','stage9_operation','stage10_security'}

async def run(args):
    registry=json.loads(args.registry.read_text(encoding='utf-8-sig'))
    assert registry['data_scope']=='synthetic_only' and registry['runtime_activation'] is False
    assert {r['role'] for r in registry['candidates']}==ROLES
    results=[]
    for row in registry['candidates']:
        if (row['endpoint'],row['model'],row['thinking'])!=('https://api.deepseek.com','deepseek-flash','disabled'):
            raise ValueError('candidate registration differs from owner instruction')
        path=Path(row['credential_file'])
        if not path.is_file():raise ValueError('candidate credential file missing')
        if not args.run:continue
        cfg=Config(row['model'],row['endpoint'],path.read_text(encoding='utf-8').strip(),'s'*32,'','')
        cfg.model_role=row['role'];cfg.model_thinking='disabled'
        async with httpx.AsyncClient(trust_env=False) as client:
            provider=Provider(cfg,client)
            for i in range(3):
                started=time.perf_counter()
                msg=await provider.complete([{'role':'system','content':'这是合成候选通道连通测试，不是运行服务。只返回JSON对象，不调用工具。'},
                    {'role':'user','content':json.dumps({'synthetic':True,'role':row['role'],'request':'return {"synthetic":true,"connected":true}'})}])
                content=json.loads(msg['content'])
                assert content.get('synthetic') is True and content.get('connected') is True
                results.append({'role':row['role'],'round':i+1,'passed':True,'latency_ms':round((time.perf_counter()-started)*1000,1),'usage':msg['usage']})
    report={'registry_valid':True,'roles':sorted(ROLES),'synthetic_transport_only':True,'runtime_services_tested':False,'production_configuration_changed':False,'results':results}
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'registry_valid':True,'role_count':len(ROLES),'transport_samples':len(results)}))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--registry',required=True,type=Path);p.add_argument('--output',required=True,type=Path);p.add_argument('--run',action='store_true')
    asyncio.run(run(p.parse_args()))
