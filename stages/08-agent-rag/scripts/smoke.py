"""Real model/search and authenticated business tools. Secrets never enter reports."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import httpx


CASES = [
    ("普通技术", "什么是TCP协议？用通俗语言解释。", "general"),
    ("生活问答", "西红柿炒蛋怎么做？给我步骤。", "life"),
    ("长回答", "写一份不少于2000字的光伏运维培训资料，详细展开原理、巡检、常见问题和学习建议。", "long"),
    ("向量资料", "请检索内部资料，解释本平台Docker容器部署的服务组成和启动方式。", "knowledge"),
    ("真实搜索", "请联网搜索最近一周的光伏技术新闻，给出最新消息和来源链接。", "web"),
    ("第二台状态", "第二台设备状态如何？", "device"),
    ("多轮指代", "它现在的温度和功率是多少？", "context"),
    ("口语状态", "帮我看看第二台现在怎么样", "device"),
    ("异常问法", "第二台有没有异常？", "device"),
    ("告警排名", "哪个设备出现的问题最多？请列出设备编码和统计时间范围。", "ranking"),
    ("组合工具", "查一下第二台设备的最新温度，再检索内部手册解释这个型号一般怎么散热，并联网找相关公开技术资料。", "combined"),
    ("库外解释", "解释钙钛矿太阳能电池中离子迁移为什么会影响稳定性。", "general"),
]


async def main(args):
    output = args.output; output.mkdir(parents=True, exist_ok=True)
    rows, sid = [], None
    async with httpx.AsyncClient(base_url=args.base_url,timeout=135,trust_env=False) as client:
        login = await client.post('/api/v1/auth/login',json={"username":os.getenv('NATURAL_TEST_USERNAME','natural_test'),"password":os.environ['NATURAL_TEST_PASSWORD']})
        login.raise_for_status(); client.headers['Authorization'] = 'Bearer '+login.json()['data']['token']
        for index,(label,question,kind) in enumerate(CASES):
            if args.case and index not in args.case: continue
            if kind != 'context': sid = None
            reply = await client.post('/api/v1/agent/chat',json={"message":question,**({"session_id":sid} if sid else {})})
            if reply.status_code == 429:
                remaining=min(60,int(reply.headers.get('Retry-After','60')))
                while remaining: await asyncio.sleep(min(5,remaining));remaining-=min(5,remaining)
                reply=await client.post('/api/v1/agent/chat',json={"message":question,**({"session_id":sid} if sid else {})})
            data=reply.json().get('data',{}) if reply.status_code==200 else {}
            sid=data.get('session_id',sid)
            text=data.get('answer_markdown',''); calls=data.get('tool_calls',[]); sources=data.get('sources',[])
            names={c['name'] for c in calls if c['status']=='ok'}
            checks={'http':reply.status_code==200,'answered':data.get('status')==('out_of_scope' if kind=='life' else 'answered') and bool(text),'no_old_contract':not any(k in data for k in ['verified_facts','source_coverage_percent','out_of_scope'])}
            if kind=='life':
                checks.update(program_boundary=data.get('response_mode')=='boundary', no_model=data.get('model_called') is False,
                              no_tools=not calls and not sources, scope_only='暂不提供内容回答' in text and '步骤' not in text)
            if kind=='long':checks['length']=len(text)>1500
            if kind in ('device','context','combined'):
                checks['telemetry']=any(c['name']=='get_telemetry' and c['status']=='ok' and c['arguments'].get('device_id')==2 for c in calls)
                checks['correct_device']='INV-1002' in text
            if kind in ('knowledge','combined'):checks['vector_tool']='search_knowledge' in names and any(s['kind']=='document' for s in sources)
            if kind in ('web','combined'):checks['web_tool']='web_search' in names and any(s['kind']=='web' for s in sources)
            if kind=='ranking':
                stats=next((c.get('result',{}) for c in calls if c['name']=='get_alarm_statistics' and c['status']=='ok'),{})
                checks['aggregation']=bool(stats.get('complete')) and 'start' in stats and 'end' in stats and 'total_events' in stats
                checks['time_basis']='30' in text and ('触发' in text or '事件' in text)
            row={'label':label,'question':question,'checks':checks,'passed':all(checks.values()),'response':data}
            rows.append(row);(output/'responses.json').write_text(json.dumps({'passed':all(r['passed'] for r in rows),'cases':rows},ensure_ascii=False,indent=2),encoding='utf-8')
            print(label,'PASS' if row['passed'] else 'FAIL',{k:v for k,v in checks.items() if not v},flush=True)
        if sid:
            history=await client.get('/api/v1/agent/sessions/'+sid)
            (output/'history.json').write_text(json.dumps(history.json().get('data',{}),ensure_ascii=False,indent=2),encoding='utf-8')
    print('passed',sum(r['passed'] for r in rows),'/',len(rows),flush=True)
    return all(r['passed'] for r in rows)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);parser.add_argument('--case',type=int,nargs='+');parser.add_argument('--base-url',default='http://127.0.0.1:8080')
    raise SystemExit(0 if asyncio.run(main(parser.parse_args())) else 1)
