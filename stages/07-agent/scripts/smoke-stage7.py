"""Real Go/Redis integration. --fixture is explicitly NOT real model acceptance."""
import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path
from uuid import uuid4
import httpx
from redis.asyncio import Redis
from agent.config import Config
from agent.sessions import Sessions, SessionError
from agent.tools import Tools


async def run(args):
    cfg=Config.load()
    if not args.fixture and cfg.model == "stage7-test-fixture":
        raise ValueError("protocol fixture cannot count as real-model validation")
    output=args.output
    output.mkdir(parents=True,exist_ok=True)
    cases=[]
    def check(name, passed):
        cases.append({"name":name,"passed":bool(passed)})
        (output/"cases.json").write_text(json.dumps({"fixture":args.fixture,"real_model_validated":False,"cases":cases},ensure_ascii=False,indent=2),encoding="utf-8")
        if not passed: raise AssertionError(name)
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8080",timeout=50,trust_env=False) as client:
        if args.fixture:
            tokens=[]
            for _ in range(2):
                username="v2m1_"+uuid4().hex[:12]
                password=secrets.token_urlsafe(32)
                registered=await client.post("/api/v1/auth/register",json={"username":username,"password":password,"real_name":"V2-M1 local test"})
                check("local test account registered",registered.status_code==201)
                login=await client.post("/api/v1/auth/login",json={"username":username,"password":password})
                tokens.append(login.json()["data"]["token"])
            token, other=tokens
        else:
            token=Path(os.environ["STAGE7_USER_TOKEN_FILE"]).read_text().strip()
            other=None
        client.headers["Authorization"]="Bearer "+token
        missing=await client.post("/api/v1/agent/chat",json={"message":"查询设备台账"},headers={"Authorization":""})
        check("missing JWT rejected",missing.status_code==401)
        parts=token.split(".")
        claims=json.loads(base64.urlsafe_b64decode(parts[1]+"="*(-len(parts[1])%4)))
        claims["exp"]=1
        payload=base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        signing=parts[0]+"."+payload
        signature=base64.urlsafe_b64encode(hmac.new(os.environ["JWT_SECRET"].encode(),signing.encode(),hashlib.sha256).digest()).decode().rstrip("=")
        expired=await client.post("/api/v1/agent/chat",json={"message":"query"},headers={"Authorization":"Bearer "+signing+"."+signature})
        check("expired JWT rejected",expired.status_code==401)
        forged=await client.post("/api/v1/agent/chat",json={"message":"查询","user_id":1})
        check("client cannot forge user identity",forged.status_code==400)
        response=await client.post("/api/v1/agent/chat",json={"message":"查询当前设备台账，附来源与时间。"})
        data=response.json()
        (output/"chat-response.json").write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8")
        check("Go to Agent to backend tools",response.status_code==200 and data["data"]["status"]=="answered")
        sid=data["data"]["session_id"]
        check("evidence returned",bool(data["data"]["evidence"]) and all(e["source"].startswith("/api/v1/") for e in data["data"]["evidence"]))
        follow=await client.post("/api/v1/agent/chat",json={"message":"重新查询设备台账","session_id":sid})
        (output/"follow-response.json").write_text(json.dumps(follow.json(),ensure_ascii=False,indent=2),encoding="utf-8")
        check("same-user multi-turn",follow.status_code==200 and follow.json()["data"]["status"]=="answered")
        if other:
            denied=await client.post("/api/v1/agent/chat",json={"message":"查询设备","session_id":sid},headers={"Authorization":"Bearer "+other})
            check("cross-user session rejected",denied.status_code==403)
        if args.fixture:
            empty=await client.post("/api/v1/agent/chat",json={"message":"no-data"})
            check("real backend missing data not invented",empty.status_code==200 and empty.json()["data"]["status"]=="unable_to_determine")
            busy_sid=str(uuid4())
            work=asyncio.create_task(client.post("/api/v1/agent/chat",json={"message":"slow-response","session_id":busy_sid}))
            await asyncio.sleep(.3)
            busy=await client.post("/api/v1/agent/chat",json={"message":"query","session_id":busy_sid})
            check("HTTP same-session concurrency rejected",busy.status_code==409)
            check("HTTP first concurrent request completes",(await work).status_code==200)
            cancelled_sid=str(uuid4())
            try:
                await client.post("/api/v1/agent/chat",json={"message":"slow-response","session_id":cancelled_sid},timeout=.3)
            except httpx.ReadTimeout:
                pass
            else:check("client cancellation triggers",False)
            await asyncio.sleep(.6)
            recovered=await client.post("/api/v1/agent/chat",json={"message":"query","session_id":cancelled_sid})
            check("client cancellation releases lock",recovered.status_code==200)
        write=await client.post("/api/v1/devices",json={"device_code":"SHOULD-NOT-CREATE"})
        check("operator write denied",write.status_code==403)
        for suffix in ("?device_id=-1","?start=bad","?start=2026-09-30T00:00:00Z&end=2026-09-29T00:00:00Z"):
            check("invalid alarm filter "+suffix,(await client.get("/api/v1/alarms"+suffix)).status_code==400)
        devices=(await client.get("/api/v1/devices")).json()["data"]["list"]
        check("device query available",bool(devices))
        first=devices[0]
        filtered=await client.get("/api/v1/devices",params={"keyword":first["device_code"]})
        check("device keyword filtering",any(d["id"]==first["id"] for d in filtered.json()["data"]["list"]))
        tools=Tools(cfg,client)
        items=[]
        for name,parameters in [("list_devices",{}),("get_telemetry",{"device_id":first["id"]}),
                                ("get_prediction",{"device_id":first["id"]}),
                                ("list_alarms",{"device_id":first["id"]}),("get_dashboard_summary",{})]:
            item=await tools.execute(name,parameters,token,"T"+str(len(items)))
            check("real tool "+name,item.status in ("ok","stale","no_data"))
            items.append(item.model_dump())
        alarms=(await client.get("/api/v1/alarms")).json()["data"]["list"]
        if alarms:
            alarm=alarms[0]
            item=await tools.execute("get_alarm_detail",{"alarm_id":alarm["id"]},token,"T5")
            check("real alarm detail",item.status=="ok")
            items.append(item.model_dump())
            boundary=await client.get("/api/v1/alarms",params={"device_id":alarm["device_id"],"start":alarm["triggered_at"],"end":"2099-01-01T00:00:00Z"})
            rows=boundary.json()["data"]["list"]
            check("alarm device and inclusive start filter",all(a["device_id"]==alarm["device_id"] for a in rows) and any(a["id"]==alarm["id"] for a in rows))
        (output/"tool-responses.json").write_text(json.dumps(items,ensure_ascii=False,indent=2),encoding="utf-8")
        redis=Redis.from_url(cfg.redis_url,decode_responses=True)
        try:
            sessions=Sessions(redis)
            owned=str(uuid4())
            actual,history=await sessions.acquire(owned,12345,"request1")
            check("Redis fresh session",actual==owned and history==[])
            try:await sessions.acquire(owned,12345,"request2")
            except SessionError as e:check("Redis concurrency lock",e.status==409)
            else:check("Redis concurrency lock",False)
            try:await sessions.acquire(owned,54321,"request3")
            except SessionError as e:check("Redis owner isolation",e.status==403)
            else:check("Redis owner isolation",False)
            await sessions.save(owned,[{"role":"user","content":str(i)} for i in range(30)])
            check("Redis TTL",0<await redis.ttl(sessions.keys(owned)[2])<=86400)
            await sessions.release(owned,"request1")
            _,history=await sessions.acquire(owned,12345,"request4")
            check("Redis history bounded",len(history)==20)
            await sessions.release(owned,"request4")
            await redis.delete(*sessions.keys(owned))
        finally:await redis.aclose()
        raw=json.dumps(data,ensure_ascii=False)
        check("response has no credentials",all(s not in raw for s in (token,cfg.service_token,cfg.api_key) if s))
        (output/"cases.json").write_text(json.dumps({"fixture":args.fixture,"real_model_validated":not args.fixture,"cases":cases},ensure_ascii=False,indent=2),encoding="utf-8")
        print(f"{len(cases)}/{len(cases)} local integration cases passed; fixture={args.fixture}")


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--fixture",action="store_true",help="register local test users; loopback only; NOT real LLM acceptance")
    parser.add_argument("--output",type=Path,required=True)
    asyncio.run(run(parser.parse_args()))
