"""Loopback-only V2-M2 acceptance. Fixtures never count as real LLM evidence.

Creates identifiable synthetic alarms in the existing local test database.
No credentials are printed or persisted in response evidence.
"""
import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
import httpx
from redis.asyncio import Redis
from agent.config import Config


def sql(statement):
    process = subprocess.run([shutil.which("docker"), "exec", "-i", "powersystem-postgres-1", "psql", "-U", "powersystem", "-d", "powersystem", "-At", "-v", "ON_ERROR_STOP=1"],
                             input=statement, text=True, capture_output=True, encoding="utf-8")
    if process.returncode:
        raise RuntimeError("local test SQL failed")
    return process.stdout.strip()


def jwt(user):
    def encode(value): return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    raw = encode({"alg":"HS256", "typ":"JWT"}) + "." + encode({"sub":str(user), "exp":int(time.time())+3600})
    return raw + "." + base64.urlsafe_b64encode(hmac.new(os.environ["JWT_SECRET"].encode(), raw.encode(), hashlib.sha256).digest()).decode().rstrip("=")


def alarm(device, ai=False, age_hours=0):
    if age_hours not in (0,48): raise ValueError('unsupported synthetic event age')
    code="INV-V2M2-"+uuid4().hex[:10]
    device=int(sql(f"INSERT INTO devices(device_code,name,dev_type,vendor,station_code,group_name) VALUES('{code}','V2-M2 synthetic test','inverter','synthetic','ST-V2M2','v2m2-test') RETURNING id;").splitlines()[0])
    database=os.getenv("TDENGINE_DATABASE","powersystem_stage2")
    url=os.getenv("TDENGINE_URL","http://127.0.0.1:6041")+"/rest/sql"
    auth=(os.getenv("TDENGINE_USER","root"),os.environ["TDENGINE_ROOT_PASSWORD"])
    queries=[f"CREATE TABLE IF NOT EXISTS {database}.t_device_{device} USING {database}.telemetry TAGS ('{code}','ST-V2M2')"]
    end=int(time.time()*1000)
    values_sql=" ".join(f"({end-(30-index)*60000},{index},220,10,{40+index},5,0,0)" for index in range(31))
    queries.append(f"INSERT INTO {database}.t_device_{device} VALUES "+values_sql)
    for query in queries:
        response=httpx.post(url,content=query,auth=auth,timeout=10,trust_env=False)
        if response.status_code!=200 or response.json().get("code")!=0:raise RuntimeError("synthetic telemetry preparation failed")
    metric = "ai_failure_risk" if ai else "temperature"
    values = "0.72,0.65" if ai else "70,60"
    if ai:
        sql(f"INSERT INTO prediction_records(device_id,window_end_ms,probability,threshold,risk_level,model_version,top_factors,source) VALUES({device},{int(time.time()*1000)},0.72,0.65,'high','v2m2-test','[]'::jsonb,'synthetic-trained');")
    raw = sql(f"INSERT INTO alarm_records(device_id,metric,level,value,threshold,status,triggered_at) VALUES({device},'{metric}','major',{values},'unhandled',now()-interval '{age_hours} hours') RETURNING id;")
    return int(raw.splitlines()[0])


def monitoring(run, index):
    return {"version":"4", "groupKey":"v2m2", "truncatedAlerts":0, "status":"firing", "receiver":"agent", "groupLabels":{}, "commonLabels":{}, "commonAnnotations":{}, "externalURL":"",
            "alerts":[{"status":"firing", "labels":{"alertname":"PowerSystemAPIDown", "severity":"critical", "job":"api", "test_run":run},
                       "annotations":{"summary":"合成监控测试：API 抓取失败"}, "startsAt":datetime.now(timezone.utc).isoformat(),
                       "endsAt":"0001-01-01T00:00:00Z", "fingerprint":f"v2m2-{run}-{index}"}]}


async def run(args):
    cfg = Config.load()
    if args.real and cfg.model == "stage7-test-fixture": raise ValueError("fixture cannot count as real-model acceptance")
    output = args.output; output.mkdir(parents=True, exist_ok=True)
    cases = []; records = []; run_id = uuid4().hex[:8]
    def check(name, passed):
        cases.append({"case":name, "passed":bool(passed)})
        if not passed: raise AssertionError(name)
    admin = int(sql("SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1").splitlines()[0])
    operator = int(sql("SELECT id FROM users WHERE role='operator' ORDER BY id LIMIT 1").splitlines()[0])
    device = int(sql("SELECT id FROM devices WHERE deleted_at IS NULL ORDER BY id LIMIT 1").splitlines()[0])
    redis = Redis.from_url(cfg.redis_url, decode_responses=True)
    alarm_ids=[]; monitor_keys=[]
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8080", timeout=50, trust_env=False, headers={"Authorization":"Bearer "+jwt(admin)}) as api, httpx.AsyncClient(timeout=50, trust_env=False) as internal:
        async def wait_result(key):
            for _ in range(120):
                matches=[]
                page=1
                while True:
                    data=(await api.get("/api/v1/agent/interpretations",params={"page_size":100,"page":page})).json()["data"]
                    matches=[i for i in data["list"] if i["event_key"]==key]
                    if matches or page*100>=data['total'] or not data['list']: break
                    page+=1
                if matches and matches[0]["task_status"] in ("completed","degraded"): return matches[0]
                await asyncio.sleep(.5)
            raise AssertionError("interpretation timeout")
        try:
            check("missing JWT rejected", (await api.get("/api/v1/agent/interpretations",headers={"Authorization":""})).status_code==401)
            check("operator model admin endpoint denied", (await api.get("/api/v1/agent/model-status",headers={"Authorization":"Bearer "+jwt(operator)})).status_code==403)
            check("unknown detail returns 404", (await api.get("/api/v1/agent/interpretations/999999999")).status_code==404)
            # Baseline must exist before creating test alarms.
            for _ in range(30):
                if sql("SELECT count(*) FROM agent_scan_cursor WHERE name='alarms'")=="1":break
                await asyncio.sleep(.2)
            if not args.real:
                await internal.post("http://127.0.0.1:8093/scenario",json={"scenario":"normal"})
            await redis.delete("stage7:model:pause")
            groups = args.groups if args.groups else (3 if args.real else 10)
            start=time.monotonic(); keys=[]
            for index in range(groups):
                for ai in (False,True):
                    identifier=alarm(device,ai);alarm_ids.append(identifier);keys.append(f"alarm:{identifier}")
                batch=monitoring(run_id,index)
                response=await internal.post("http://127.0.0.1:8092/internal/agent/monitor-events",headers={"Authorization":"Bearer "+cfg.monitor_token},json=batch)
                check(f"monitor accepted {index}",response.status_code==200)
                key="monitor:"+batch["alerts"][0]["fingerprint"]+":"+datetime.fromisoformat(batch["alerts"][0]["startsAt"]).strftime("%Y-%m-%dT%H:%M:%S.%f").rstrip("0")+"Z"
                keys.append(key);monitor_keys.append(key)
                # Repeat payload must not create a second interpretation.
                await internal.post("http://127.0.0.1:8092/internal/agent/monitor-events",headers={"Authorization":"Bearer "+cfg.monitor_token},json=batch)
            for key in keys:
                item=await wait_result(key);records.append(item)
                valid=item["result"]["status"]=="answered"
                rejected=args.real and args.allow_validation_degradation and item['reason']=='answer_validation_failed' and item['result']['conclusion'] is None and not item['result']['suggestions']
                check(("answered " if valid else "safe validation rejection ")+key,valid or rejected)
                check("snapshot evidence "+key,bool(item["evidence"]) and (not valid or bool(item["result"]["conclusion"]["evidence_ids"])))
            elapsed=time.monotonic()-start
            check("event key uniqueness",all(sql("SELECT count(*) FROM agent_interpretations WHERE event_key='"+key+"'")=="1" for key in keys))
            ai_records=[i for i in records if i["category"]=="prediction_risk"]
            if args.real:
                for category in ('device_alarm','prediction_risk','platform_monitor'):
                    check('at least three real answers '+category,sum(i['category']==category and i['result']['status']=='answered' for i in records)>=3)
            check("trigger prediction matched",all(any(e["tool"]=="get_prediction" and e["status"]=="ok" for e in i["evidence"]) for i in ai_records))
            item=records[0]
            check("manual alarm acknowledgment",(await api.post(f"/api/v1/alarms/{item['alarm_id']}/ack")).status_code==200)
            check("duplicate acknowledgment conflict",(await api.post(f"/api/v1/alarms/{item['alarm_id']}/ack")).status_code==409)
            check("read receipt",(await api.post(f"/api/v1/agent/interpretations/{item['id']}/read")).status_code==200)
            check("read idempotent",(await api.post(f"/api/v1/agent/interpretations/{item['id']}/read")).status_code==200)
            own=(await api.get(f"/api/v1/agent/interpretations/{item['id']}")).json()["data"]
            other=(await api.get(f"/api/v1/agent/interpretations/{item['id']}",headers={"Authorization":"Bearer "+jwt(operator)})).json()["data"]
            check("per-user read isolation",own["read"] and not other["read"])
            if not args.real:
                for scenario,reason in (("quota","quota_exhausted"),("expired","auth_failed"),("limited","rate_limited")):
                    await redis.delete("stage7:model:pause")
                    await internal.post("http://127.0.0.1:8093/scenario",json={"scenario":scenario})
                    identifier=alarm(device);alarm_ids.append(identifier)
                    failure=await wait_result(f"alarm:{identifier}")
                    check(scenario+" safe degradation",failure["reason"]==reason and failure["result"]["conclusion"] is None)
                    count=(await internal.get("http://127.0.0.1:8093/scenario")).json()["requests"]
                    check(scenario+" bounded attempts",count==(3 if scenario=="limited" else 1))
                    if scenario!="limited":
                        identifier=alarm(device);alarm_ids.append(identifier)
                        await wait_result(f"alarm:{identifier}")
                        check(scenario+" paused requests",(await internal.get("http://127.0.0.1:8093/scenario")).json()["requests"]==count)
                await internal.post("http://127.0.0.1:8093/scenario",json={"scenario":"normal"})
            await redis.delete("stage7:model:probe-limit")
            check("admin probe restores",(await api.post("/api/v1/agent/model-probe")).status_code==200)
            check("probe minute limit",(await api.post("/api/v1/agent/model-probe")).status_code==429)
            if not args.real:
                check("model pause cleared",await redis.get("stage7:model:pause") is None)
            check("business ping unaffected",(await api.get("/api/v1/ping")).status_code==200)
            raw=json.dumps(records,ensure_ascii=False)
            check("no credentials in records",all(secret not in raw for secret in (cfg.api_key,cfg.service_token,cfg.monitor_token) if secret))
            (output/"interpretations.json").write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding="utf-8")
            durations=[(datetime.fromisoformat(i["updated_at"].replace("Z","+00:00"))-datetime.fromisoformat(i["occurred_at"].replace("Z","+00:00"))).total_seconds() for i in records]
            durations.sort();p95=durations[min(len(durations)-1,int(len(durations)*.95))]
            answered=sum(i['result']['status']=='answered' for i in records)
            (output/"summary.json").write_text(json.dumps({"real_model":args.real,"cases":cases,"record_count":len(records),"answered_count":answered,"validation_rejected_count":len(records)-answered,"batch_seconds":elapsed,"p95_seconds":p95,"run_id":run_id},ensure_ascii=False,indent=2),encoding="utf-8")
            print(f"V2-M2 {len(cases)}/{len(cases)} checks passed; real_model={args.real}; answered={answered}/{len(records)}; batch={elapsed:.2f}s; p95={p95:.2f}s")
        finally:
            # Keep identifiable acceptance evidence, close synthetic alarms to avoid replay.
            if alarm_ids:
                sql("UPDATE alarm_records SET status='recovered',recovered_at=now() WHERE id IN ("+",".join(map(str,alarm_ids))+");")
                sql("UPDATE devices SET deleted_at=now() WHERE group_name='v2m2-test' AND id IN (SELECT device_id FROM alarm_records WHERE id IN ("+",".join(map(str,alarm_ids))+"));")
            if monitor_keys:
                sql("UPDATE agent_monitor_events SET recovered_at=now() WHERE event_key IN ("+",".join("'"+key+"'" for key in monitor_keys)+");")
            (output/"cases.json").write_text(json.dumps(cases,ensure_ascii=False,indent=2),encoding="utf-8")
            await redis.aclose()


if __name__ == "__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--real",action="store_true")
    parser.add_argument("--allow-validation-degradation",action="store_true",help="record real-model rejection separately; still requires three successful answers per category")
    parser.add_argument("--groups",type=int,choices=range(1,11),help="each group creates device, AI and monitor events")
    parser.add_argument("--output",type=Path,required=True)
    asyncio.run(run(parser.parse_args()))
