"""Validate a separately launched Agent with its primary model unreachable."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import httpx
from agent.config import Config


async def main(args):
    cfg = Config.load()
    rows = []
    async with httpx.AsyncClient(trust_env=False,timeout=45) as client:
        login = await client.post("http://127.0.0.1:8080/api/v1/auth/login",json={"username":os.environ.get("NATURAL_TEST_USERNAME","natural_test"),"password":os.environ["NATURAL_TEST_PASSWORD"]})
        login.raise_for_status()
        identity = login.json()["data"]
        headers = {"Authorization":"Bearer "+identity["token"],"X-PowerSystem-Token":cfg.service_token}
        for question, status in [("第二台设备状态如何？","answered"), ("帮我看看第二台现在怎么样","answered"),
                                 ("第二台有没有异常？","answered"), ("查询所有设备列表","answered"),
                                 ("哪个设备出现的问题最多？","answered"), ("INV-1002还有告警吗？","answered"), ("风机覆冰机理是什么？","degraded")]:
            reply = await client.post(args.url+"/internal/agent/chat",headers=headers,json={"message":question,"user_id":identity["user"]["id"]})
            data = reply.json()
            passed = reply.status_code==200 and data.get("status")==status
            if status=="answered":
                passed = passed and bool(data.get("verified_facts")) and not data.get("unverified_explanations")
                if question=="INV-1002还有告警吗？":
                    passed = passed and "无触发时间限制" in data.get("conclusion",{}).get("text","")
            else:
                passed = passed and not data.get("conclusion") and not data.get("verified_facts")
            rows.append({"question":question,"passed":bool(passed),"response":data})
            print(question,"passed" if passed else "failed",flush=True)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps({"primary_model_unreachable":True,"fallback_model":False,"passed":all(row["passed"] for row in rows),"cases":rows},ensure_ascii=False,indent=2),encoding="utf-8")
    return all(row["passed"] for row in rows)


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--url",default="http://127.0.0.1:18092")
    parser.add_argument("--output",type=Path,required=True)
    raise SystemExit(0 if asyncio.run(main(parser.parse_args())) else 1)
