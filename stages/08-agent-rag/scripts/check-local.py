"""Local-only controlled failures, semantic vector probes and platform regression."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import httpx
from rag_agent.config import Config
from rag_agent.engine import Engine
from rag_agent.knowledge import Knowledge
from rag_agent.provider import Provider
from rag_agent.tools import Tools


async def main():
    cfg = Config.load(); checks = {}; details = {}
    def initial(): return dict(status="degraded",answer_markdown="",sources=[],tool_calls=[],warnings=[])
    # Actual provider protocol against a controlled failure, without sending data externally.
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(503,json={"error":"fixture"}))) as client:
        out = initial()
        await Engine(cfg,Provider(cfg,client),Tools(cfg,client,None)).run("技术问题",[],"fixture",out)
        checks["model_503_is_degraded"] = out["status"] == "degraded" and not out["answer_markdown"] and "503" in str(out["warnings"])
        details["model_failure"] = out
        knowledge = Knowledge(cfg,client,embedding=type("Embedding",(),{"encode":lambda self,*args:[[0.0]*384]})())
        tools = Tools(cfg,client,knowledge)
        for name,args in [("search_knowledge",'{"query":"资料"}'),("web_search",'{"query":"测试问题"}')]:
            out = initial(); await tools.execute(name,args,"fixture",out)
            checks[name+"_503_no_fabricated_sources"] = bool(out["warnings"]) and not out["sources"] and out["tool_calls"][0]["status"] == "error"
    async with httpx.AsyncClient(trust_env=False,timeout=30) as client:
        knowledge = Knowledge(cfg,client)
        for index, query in enumerate(["平台有哪些容器，如何拉起服务", "现场逆变器的风扇冷却方式是什么"]):
            rows = await knowledge.search(query)
            checks["semantic_query_"+str(index)] = len(rows)==5 and any(("部署" in r["document"] or "Docker" in r["content"]) if index==0 else ("SUN2000" in r["content"] or "散热" in r["content"]) for r in rows)
            details["semantic_"+str(index)] = [{"id":r["id"],"document":r["document"],"heading":r["heading"],"score":r["score"]} for r in rows]
        reply=await client.post(cfg.qdrant_url+f"/collections/{cfg.collection}/points/scroll",json={"limit":1000,"with_payload":True,"with_vector":False})
        reply.raise_for_status(); points=reply.json()["result"]["points"]
        tokenizer=knowledge.embedding.load().tokenizer
        counts=[len(tokenizer.encode(p["payload"]["content"],add_special_tokens=False,verbose=False)) for p in points]
        checks["all_chunks_at_most_400_tokens"] = max(counts)<=400
        details["chunks"]={"count":len(counts),"maximum_tokens":max(counts)}
        login=await client.post(cfg.backend_url+"/api/v1/auth/login",json={"username":os.getenv("NATURAL_TEST_USERNAME","natural_test"),"password":os.environ["NATURAL_TEST_PASSWORD"]})
        login.raise_for_status(); headers={"Authorization":"Bearer "+login.json()["data"]["token"]}
        for name,path in [("devices","/api/v1/devices?page_size=100"),("alarms","/api/v1/alarms?page_size=10"),("telemetry","/api/v1/devices/2/telemetry/latest"),("prediction","/api/v1/devices/2/prediction"),("statistics","/api/v1/alarms/statistics")]:
            reply=await client.get(cfg.backend_url+path,headers=headers)
            checks["legacy_"+name]=reply.status_code==200
            details["legacy_"+name]={"http_status":reply.status_code,"code":reply.json().get("code")}
        index=json.loads(Path('stages/07-agent/knowledge/index.json').read_text(encoding='utf-8'))
        for name,path in [("knowledge","/api/v1/agent/knowledge/"+index['chunks'][0]['id']),
                          ("interpretations","/api/v1/agent/interpretations")]:
            reply=await client.get(cfg.backend_url+path,headers=headers)
            checks['legacy_'+name]=reply.status_code==200
    output=Path("artifacts/stage8-agent-rag/rebuild-20261011/local-checks.json")
    output.write_text(json.dumps({"passed":all(checks.values()),"checks":checks,"details":details},ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(checks,ensure_ascii=False)); return all(checks.values())


if __name__ == "__main__": raise SystemExit(0 if asyncio.run(main()) else 1)
