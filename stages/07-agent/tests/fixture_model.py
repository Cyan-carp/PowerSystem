"""Local protocol fixture, never included in the Agent image or used as a real LLM."""
import json
import asyncio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app=FastAPI()
scenario="normal"
requests_count=0


@app.post("/scenario")
async def set_scenario(request: Request):
    global scenario, requests_count
    scenario=(await request.json()).get("scenario", "normal")
    requests_count=0
    return {"scenario":scenario}


@app.get("/scenario")
async def get_scenario():
    return {"scenario":scenario,"requests":requests_count}


@app.post("/v1/chat/completions")
async def complete(request: Request):
    global requests_count
    requests_count+=1
    if scenario=="quota":
        return JSONResponse(status_code=429,content={"error":{"code":"insufficient_quota"}})
    if scenario=="expired":
        return JSONResponse(status_code=401,content={"error":{"code":"api_key_expired"}})
    if scenario=="limited":
        return JSONResponse(status_code=429,content={"error":{"code":"rate_limit_exceeded"}})
    data=await request.json()
    if data.get("model")!="stage7-test-fixture":
        return JSONResponse(status_code=400,content={"error":"fixture model only"})
    messages=data["messages"]
    if "tools" not in data:
        try:
            event=json.loads(messages[-1]["content"])
        except (ValueError,KeyError):
            event={}
        if "evidence" in event:
            item=event["evidence"][0]
            answer={"conclusion":{"text":"事件证据记录了告警触发现象，具体原因需要人工核查。","evidence_ids":[item["id"]]},
                    "suggestions":[{"text":"核对事件原值及相关设备状态","evidence_ids":[item["id"]]}],"limitations":[]}
            message={"role":"assistant","content":json.dumps(answer,ensure_ascii=False)}
        else:
            message={"role":"assistant","content":"OK (protocol fixture)"}
    elif data["tools"][0]["function"]["name"]=="submit_interpretation":
        item=json.loads(messages[-1]["content"])["evidence"][0]
        answer={"conclusion":{"text":"事件证据记录了告警触发现象，具体原因需要人工核查。","evidence_ids":[item["id"]]},
                "suggestions":[{"text":"核对事件原值及相关设备状态","evidence_ids":[item["id"]]}],"limitations":[]}
        message={"role":"assistant","content":None,"tool_calls":[{"id":"output1","type":"function","function":{"name":"submit_interpretation","arguments":json.dumps(answer,ensure_ascii=False)}}]}
    elif data["tools"][0]["function"]["name"]=="probe":
        message={"role":"assistant","content":None,"tool_calls":[{"id":"probe1","type":"function","function":{"name":"probe","arguments":'{"value":7}'}}]}
    elif messages[-1]["role"]=="tool":
        item=json.loads(messages[-1]["content"])
        answer={"conclusion":{"text":"已取得当前查询的设备台账证据。","evidence_ids":[item["id"]]},
                "suggestions":[{"text":"人工核对设备台账和原页面数据","evidence_ids":[item["id"]]}],"limitations":[]}
        message={"role":"assistant","content":json.dumps(answer,ensure_ascii=False)}
    else:
        text=messages[-1]["content"]
        if "slow-response" in text:
            await asyncio.sleep(2)
        name="get_prediction" if "no-data" in text else "list_devices"
        args={"device_id":999999999} if name=="get_prediction" else {"page":1,"page_size":20}
        message={"role":"assistant","content":None,"tool_calls":[{"id":"fixture1","type":"function","function":{"name":name,"arguments":json.dumps(args)}}]}
    return {"choices":[{"message":message}],"model":"stage7-test-fixture"}
