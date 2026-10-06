"""Separate event wait, Agent processing and isolated model replay timings."""
import argparse
import asyncio
import hashlib
import json
import math
import time
from datetime import datetime
from pathlib import Path
import httpx
from agent.config import Config
from agent.interpret import Interpretation, SYSTEM, OUTPUT_SCHEMA, validate_event_answer
from agent.llm import Provider, ProviderError
from agent.schemas import ModelAnswer


def distribution(values):
    if not values:return {'count':0,'p50_seconds':None,'p95_seconds':None}
    ordered=sorted(values)
    return {'count':len(values),'p50_seconds':round(ordered[math.ceil(len(values)*.5)-1],3),
            'p95_seconds':round(ordered[math.ceil(len(values)*.95)-1],3)}


async def main(args):
    if args.output.exists():raise FileExistsError('new performance evidence required')
    rows=json.loads(args.interpretations.read_text(encoding='utf-8'))
    audits={x['request_id']:x for path in args.audit.glob('*.jsonl') for line in path.read_text(encoding='utf-8').splitlines() if (x:=json.loads(line)).get('event_key')}
    waiting=[];processing=[]
    for row in rows:
        audit=audits.get(row['result']['request_id'])
        if audit:processing.append(audit['elapsed_ms']/1000)
        collected=[datetime.fromisoformat(e['collected_at'].replace('Z','+00:00')) for e in row['evidence'] if e.get('collected_at')]
        if collected:waiting.append(max(0,(max(collected)-datetime.fromisoformat(row['occurred_at'].replace('Z','+00:00'))).total_seconds()))
    replay=[]
    if args.replay:
        cfg=Config.load()
        async with httpx.AsyncClient(trust_env=False) as client:
            provider=Provider(cfg,client)
            for category in sorted({x['category'] for x in rows}):
                for row in [x for x in rows if x['category']==category][:3]:
                    body=Interpretation(event_key=row['event_key'],category=category,evidence=row['evidence'])
                    started=time.monotonic();status='failed';reason='';digest=None
                    try:
                        result=await provider.complete([{'role':'system','content':SYSTEM},{'role':'user','content':body.model_dump_json()}],OUTPUT_SCHEMA)
                        model_seconds=time.monotonic()-started
                        content=result.get('content')
                        if result.get('tool_calls'):content=result['tool_calls'][0]['function']['arguments']
                        validate_event_answer(ModelAnswer.model_validate_json(content),body.evidence,body.category)
                        status='answered';digest=hashlib.sha256(json.dumps(result,sort_keys=True).encode()).hexdigest()
                    except (ProviderError,ValueError,KeyError,TypeError) as error:
                        model_seconds=time.monotonic()-started;reason=str(error) if isinstance(error,ProviderError) else 'validation_rejected'
                    replay.append({'id':row['id'],'category':category,'status':status,'reason':reason,'model_seconds':round(model_seconds,3),'response_sha256':digest})
    report={'completed':True,'event_queue_and_snapshot':distribution(waiting),'agent_processing':distribution(processing),
        'isolated_model_replay':distribution([x['model_seconds'] for x in replay if x['status']=='answered']),
        'model_replay':replay,'page_display':{'verified':False,'reason':'requires actual browser timestamp evidence'},
        'budgets_seconds':{'model':20,'agent':40,'go':45},
        'measurement_boundary':'event wait ends at snapshot collection; Agent includes validation/audit setup; model replay uses saved synthetic snapshots independently and does not measure production queue/render'}
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:report[k] for k in ('event_queue_and_snapshot','agent_processing','isolated_model_replay')}))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--interpretations',type=Path,required=True);p.add_argument('--audit',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--replay',action='store_true');asyncio.run(main(p.parse_args()))
