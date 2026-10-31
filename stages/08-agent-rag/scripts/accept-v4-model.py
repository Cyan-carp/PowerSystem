"""Independent real-candidate evaluation for V4-S02.

Runs Q01-Q10 synthetic fixtures against the configured candidate model with
mock tools. It never auto-calls the service on import and never sends repository
or production data. Each sample is a single attempt: no retry, no fallback.
Quality still requires manual scoring; this script only collects evidence.

Usage:
  python scripts/accept-v4-model.py --check-only
  python scripts/accept-v4-model.py --run --rounds 3 --output <path>
"""
import argparse
import asyncio
import json
import os
import re
import sys
import time
import hashlib
import math
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rag_agent.config import Config
from rag_agent.provider import ModelError, Provider
from rag_agent.engine import Engine
from rag_agent.ops import SupervisorTools
from rag_agent.business_quality import build_quality
from uuid import UUID
from types import SimpleNamespace
import httpx

SYSTEM = ("这是离线合成模型评测，不是生产会话。下面所有输入与工具返回值均为固定合成夹具，"
          "不代表任何真实设备或用户数据。请按系统提示正常回答；需要工具时调用工具。")

FIXTURES = [
    {"id": "Q01", "prompt": "设备日常巡检需要检查什么？请提供相关资料。"},
    {"id": "Q02", "prompt": "第二台设备状态如何？请说明当前遥测、预测和告警，并注明数据时间。"},
    {"id": "Q03", "prompt": "第二台设备近7天汇总发电量是多少？请说明统计窗口。"},
    {"id": "Q04", "prompt": "上面那个任务失败了，失败原因是什么？"},
    {"id": "Q05", "prompt": "“证据不足，无法给出结论”这句话什么意思？"},
    {"id": "Q06", "prompt": "全部设备最新一条告警是什么？按时间降序只看第一条。"},
    {"id": "Q07", "prompt": "导出今天设备可用率报表。"},
    {"id": "Q08", "prompt": "先查第二台设备，再查第一台，然后说明两台差异。"},
    {"id": "Q09", "prompt": "解释一下逆变器MPPT原理，顺便给一个晚餐搭配建议；今天会下雨吗？谢谢。"},
    {"id": "Q10", "prompt": "你能做什么？哪些平台功能是助手可以执行的？"},
]

MOCK_RESULTS = {'search_knowledge': {'synthetic': True, 'results': [{
    'id':'KSYN0000000000000001','document':'合成巡检指引.md','heading':'巡检项目',
    'content':'合成夹具：检查连接、散热、告警与发电趋势。','version':'synthetic-v1',
    'source_hash':'0'*64,'collection':'synthetic_collection','score':0.9}]}}

REQUIRED_ENV = ("AGENT_LLM_MODEL", "AGENT_LLM_BASE_URL", "AGENT_SERVICE_TOKEN_FILE",
                "AGENT_LLM_API_KEY_FILE", "RAG_DATABASE_URL", "AGENT_REDIS_URL")


def config_readiness():
    missing = [name for name in REQUIRED_ENV
               if not os.getenv(name) or (name.endswith("_FILE") and not Path(os.getenv(name)).is_file())]
    return {"ready": not missing, "missing": missing}


def mock_tool(name, arguments):
    result = MOCK_RESULTS.get(name)
    if result is None:
        return {"synthetic": True, "error": "unknown_synthetic_tool"}
    return result


def leak_check(text, cfg):
    if not isinstance(text, str): return True
    values = [cfg.llm_key, cfg.service_token, cfg.search_key]
    if any(value and value in text for value in values): return False
    return not re.search(r"\bsk-[A-Za-z0-9_-]{16,}", text)


def case_record(case, index):
    return {"case": case["id"], "round": index, "status": "ok", "output": None,
            "steps": [], "latency_ms": None, "usage": None,
            "cost": {"amount": None, "currency": None, "reason": "pricing_not_configured"},
            "finish_reason": None, "error_class": None, "error": None,
            "auto_checks": {"non_empty": False, "no_secret_leak": True,
                            "tool_arguments_valid": True}}


STAMP = "2026-10-23T02:00:00Z"
TASK_ID = "00000000-0000-4000-8000-000000000002"


def synthetic_call(name, data, args=None, status="ok"):
    return {"name":name,"arguments":args or {},"result":data,"status":status,"collected_at":STAMP}


class FixtureTools:
    def __init__(self, cfg): self.cfg=cfg
    async def execute(self,name,arguments,jwt,result):
        args=json.loads(arguments)
        if name != "search_knowledge":
            return {"error":"synthetic_tool_unavailable"}
        data=MOCK_RESULTS[name]
        result["tool_calls"].append(synthetic_call(name,data,args))
        result["sources"].append({"kind":"document","url":"/synthetic/K1","id":"KSYN0000000000000001","version":"synthetic-v1"})
        return data


class FixtureOps:
    def __init__(self,cfg,case):
        self.cfg,self.case=cfg,case
        self.tasks={TASK_ID:self.failed()} if case['id']=='Q04' else {}
        self.sequence=100
    async def dispatch(self,intent,target=None,**kwargs):
        assert kwargs.get('operation','read_only')=='read_only'
        if self.case['id']=='Q03':
            calls=[synthetic_call('list_devices',{'list':[{'id':11,'device_code':'SYN-INV-0001'},{'id':28,'device_code':'SYN-INV-0002'}],'total':2,'order':'id ASC'}),
                synthetic_call('get_energy_report',{
                    'device_id':28,'devices':[{'device_id':28,'device_code':'SYN-INV-0002'}],
                    'start_date':'2026-10-17','end_date':'2026-10-24','days':7,
                    'daily':[{'date':f'2026-10-{d:02d}','kwh':float(d-16)*10,'complete':True,'device_count':1,'valid_device_count':1} for d in range(17,24)],
                    'total_kwh':280.0,'aggregated_at':'2026-10-23T16:01:00Z','complete':True,'missing':[],
                    'rule_version':'energy-daily-v1','scope':'all','station_code':''},
                    {'device_id':28,'start_date':'2026-10-17','end_date':'2026-10-24','scope':'all'})]
            calls[1]['collected_at']='2026-10-23T16:02:00Z'
            self.sequence+=1
            tid=str(UUID(int=self.sequence))
            task={'task_id':tid,'status':'succeeded','tool_calls':calls,'sources':[],'business_quality':build_quality(calls)}
            self.tasks[tid]=task
            return task
        if self.case['id']=='Q06':
            calls=[synthetic_call('list_alarms',{'list':[{'id':13,'device_id':2,'status':'unhandled','triggered_at':STAMP}], 'total':3,'page':1},{'page':1,'page_size':1})]
        else:
            calls=[synthetic_call('list_devices',{'list':[{'id':11,'device_code':'SYN-INV-0001'},{'id':28,'device_code':'SYN-INV-0002'}],'total':2}),
                synthetic_call('get_device',{'id':28,'device_code':'SYN-INV-0002'},{'device_id':28}),
                synthetic_call('get_telemetry',{'device_id':28,'power':512.4,'status':1,'ts_ms':1792720800000,'received_at':STAMP},{'device_id':28}),
                synthetic_call('get_prediction',{'device_id':28,'probability':0.2,'threshold':0.6,'risk_level':'low','stale':False,'model_version':'synthetic-v1','window_end_ms':1792720800000},{'device_id':28}),
                synthetic_call('list_alarms',{'list':[],'total':0,'page':1},{'device_id':28}),
                synthetic_call('get_alarm_rules',{'list':[],'total':0,'page':1},{'device_id':28})]
        if re.search(r'第一(?:台|项)|第\s*1\s*(?:台|项)',intent):
            for call in calls[1:]:
                if 'device_id' in call['arguments']: call['arguments']['device_id']=11
                if 'device_id' in call['result']: call['result']['device_id']=11
                if call['name']=='get_device':call['result'].update(id=11,device_code='SYN-INV-0001')
                if call['name']=='get_telemetry':call['result']['power']=256.2
        self.sequence+=1
        tid=str(UUID(int=self.sequence))
        task={'task_id':tid,'status':'succeeded','tool_calls':calls,'sources':[], 'business_quality':build_quality(calls)}
        self.tasks[tid]=task
        return task
    def failed(self,code='required_tool_failed'):
        return {'task_id':TASK_ID,'status':'failed','error':{'code':code},'sources':[],
            'tool_calls':[{'name':'platform.get_telemetry','status':'error','error':'HTTP 503','arguments':{'device_id':28},'collected_at':STAMP}]}
    async def wait(self,task,seconds):return task
    async def get(self,tid):
        if str(tid) not in self.tasks: raise ValueError('unknown_synthetic_task')
        return self.tasks[str(tid)]


async def run_sample(cfg, provider, case, index, max_steps):
    record = case_record(case, index)
    class Recorder:
        async def complete(self,messages,schemas=None):
            begin=time.perf_counter()
            response=await provider.complete(messages,schemas)
            record['steps'].append({'latency_ms':round((time.perf_counter()-begin)*1000,1),
                'content':response.get('content'),'tool_calls':response.get('tool_calls',[]),
                'usage':response.get('usage'),'finish_reason':response.get('finish_reason')})
            return response
    previous=[]
    if case['id']=='Q04':
        previous=[{'question':'第二台设备近7天汇总发电量','model_messages':[],
            'response':{'task_cards':[{'task_id':TASK_ID,'status':'failed'}]}}]
    prompts=(["第二台设备状态如何", "第一台设备状态如何", "这两台设备有什么差异"]
             if case['id']=='Q08' else [case['prompt']])
    started=time.perf_counter();turns=[]
    ops=FixtureOps(cfg,case)
    cfg.v4_assistant_enabled=True
    cfg.max_rounds=min(cfg.max_rounds,max_steps)
    try:
        for question in prompts:
            result={'status':'degraded','answer_markdown':'','tool_calls':[], 'sources':[], 'warnings':[]}
            tools=SupervisorTools(FixtureTools(cfg),ops,time.monotonic()+cfg.total_timeout)
            messages=await Engine(cfg,Recorder(),tools).run(question,previous,'synthetic-jwt',result)
            turns.append({'question':question,'response':result})
            previous.append({'question':question,'response':result,'model_messages':messages})
        record['output']=result['answer_markdown'];record['turns']=turns
        record['status']='ok' if all(t['response']['status']=='answered' for t in turns) else 'error'
        if record['status']=='error':
            failed=next(t['response'] for t in turns if t['response']['status']!='answered')
            record['error']=failed.get('error',{}).get('message')
            record['error_class']=failed.get('error',{}).get('code')
        record['usage']={k:sum(s.get('usage',{}).get(k,0) for s in record['steps'] if s.get('usage'))
                         for k in ('prompt_tokens','completion_tokens','total_tokens')} if any(s.get('usage') for s in record['steps']) else None
        record['finish_reason']=record['steps'][-1]['finish_reason'] if record['steps'] else None
    except ModelError as error:
        record.update(status='error',error_class=error.category,error=str(error))
    record['latency_ms']=round((time.perf_counter()-started)*1000,1)
    record['auto_checks']['non_empty']=bool(record['output'])
    record['auto_checks']['no_secret_leak']=leak_check(record['output'],cfg)
    return record


def build_fixtures():
    assert len(FIXTURES) == 10 and len({row["id"] for row in FIXTURES}) == 10
    for row in FIXTURES:
        assert row["id"].startswith("Q") and row["prompt"].strip()
        assert "http://" not in row["prompt"] and "https://" not in row["prompt"]
    return FIXTURES


async def run(args):
    build_fixtures()
    cfg = Config.load()
    output = args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    provider = Provider(cfg, httpx.AsyncClient(trust_env=False, timeout=cfg.model_timeout + 5))
    started = datetime.now(timezone.utc).isoformat()
    source_hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in [Path(__file__),* (Path(__file__).resolve().parents[1]/"rag_agent").glob("*.py")]}
    results = []
    try:
        for case in [c for c in FIXTURES if not args.cases or c["id"] in args.cases.split(",")]:
            for index in range(1, args.rounds + 1):
                record = await run_sample(cfg, provider, case, index, args.max_steps)
                results.append(record)
                output.with_suffix(".partial.json").write_text(json.dumps({"model_profile":cfg.provider_profile(),"results":results,"verdict":"in_progress"},ensure_ascii=False,indent=2),encoding="utf-8")
                print(json.dumps({"case": record["case"], "round": index,
                                  "status": record["status"]}), flush=True)
    finally:
        await provider.client.aclose()
    finished = datetime.now(timezone.utc).isoformat()
    completed = sum(row["status"] == "ok" for row in results)
    report = {"mode": "real_candidate_eval", "started_at": started, "finished_at": finished,
              "model_profile": cfg.provider_profile(), "fixtures": len(FIXTURES),
              "rounds": args.rounds, "results": results, "source_hashes": source_hashes,
              "latency_by_case": {c:{"samples":len(v),"p50_ms":sorted(v)[math.ceil(.5*len(v))-1],"p95_ms":sorted(v)[math.ceil(.95*len(v))-1]} for c in {r["case"] for r in results} if (v:=[r["latency_ms"] for r in results if r["case"]==c])},
              "summary": {"samples": len(results), "completed": completed,
                          "errors": len(results) - completed,
                          "cost": {"amount": None, "currency": None,
                                   "reason": "pricing_not_configured"}},
              "verdict": "collected_requires_manual_scoring",
              "note": "Structural checks only; correctness/completeness/readability need manual 0-2 scoring."}
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"samples": len(results), "completed": completed, "output": str(output)}))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases",default="",help="comma separated Q case IDs")
    parser.add_argument("--run", action="store_true", help="perform real candidate calls")
    parser.add_argument("--check-only", action="store_true", help="validate fixtures/config only")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--max-steps", type=int, default=8)
    parser.add_argument("--output", type=Path,
                        default=Path("artifacts/v4-智能助手/S02-回答质量-20261023-r1/model-eval.json"))
    args = parser.parse_args(argv)
    if args.rounds < 3 or args.max_steps < 1:
        parser.error("rounds must be at least 3 and max-steps must be positive")
    fixtures = build_fixtures()
    if not args.run:
        print(json.dumps({"mode": "check_only", "fixtures": len(fixtures),
                          "ids": [row["id"] for row in fixtures],
                          "config": config_readiness()}, ensure_ascii=False))
        return 0
    if not config_readiness()["ready"]:
        print(json.dumps({"error": "private_config_missing", **config_readiness()}, ensure_ascii=False))
        return 2
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
