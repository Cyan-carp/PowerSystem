"""Real model, authenticated Go API, per-turn tools and facts acceptance.

Run only on the isolated local environment prepared for this upgrade. Reports
contain synthetic test questions, never JWTs or database/model credentials.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import httpx

ROOT = Path(__file__).resolve().parents[3]


async def main(args):
    args.output.mkdir(parents=True, exist_ok=True)
    cases = json.loads((ROOT/"stages/07-agent/tests/natural-cases.json").read_text(encoding="utf-8"))
    if args.case is not None:
        cases = [cases[index] for index in args.case]
    rows = []
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8080",trust_env=False,timeout=50) as client:
        credentials = {"username":os.environ.get("NATURAL_TEST_USERNAME","natural_test"),"password":os.environ["NATURAL_TEST_PASSWORD"]}
        if args.create_user:
            created = await client.post("/api/v1/auth/register",json={**credentials,"real_name":"本地自然问法测试"})
            if created.status_code not in (201,409): raise RuntimeError("test_user_creation_failed")
        login = await client.post("/api/v1/auth/login",json=credentials)
        login.raise_for_status()
        client.headers["Authorization"] = "Bearer "+login.json()["data"]["token"]
        sid = None
        for index, case in enumerate(cases):
            if not case.get("context"):
                sid = None
            result = await client.post("/api/v1/agent/chat",json={"message":case["question"],**({"session_id":sid} if sid else {})})
            if result.status_code == 429:
                await asyncio.sleep(min(60,int(result.headers.get("Retry-After",60))))
                result = await client.post("/api/v1/agent/chat",json={"message":case["question"],**({"session_id":sid} if sid else {})})
            checks = {"http":result.status_code==200}
            data = result.json().get("data",{}) if result.status_code == 200 else {}
            sid = data.get("session_id",sid)
            if case.get("scope") == "out_of_scope":
                checks.update(scope=data.get("status")=="out_of_scope",no_sources=not data.get("evidence"),no_facts=not data.get("verified_facts"))
            else:
                checks["answered"] = data.get("status")=="answered" and bool(data.get("conclusion"))
                evidence = data.get("evidence",[])
                if case["intent"] in ("status","metric","prediction"):
                    tool = {"status":"get_telemetry","metric":"get_telemetry","prediction":"get_prediction"}[case["intent"]]
                    checks["fresh_required_tool"] = any(e["tool"]==tool and e["status"]=="ok" and e["data"].get("device_id")==2 for e in evidence)
                    checks["device_resolved"] = any(d.get("id")==2 and d.get("device_code")=="INV-1002" for d in data.get("resolved_devices",[]))
                if case["intent"] == "ranking":
                    checks["aggregate_used"] = any(e["tool"]=="get_alarm_statistics" and e["status"]=="ok" and e["data"].get("complete") is True for e in evidence)
                    text = (data.get("conclusion") or {}).get("text","")
                    checks["basis_and_window"] = "triggered_at" in text and "统计范围" in text and "包含已恢复和未恢复" in text
                if case["intent"] == "alarms":
                    checks["current_alarm_counts"] = sum(e["tool"]=="list_alarms" and e["status"] in ("ok","no_data") and e["data"].get("total")==1 for e in evidence)==2
                    checks["device_resolved"] = any(d.get("id")==2 for d in data.get("resolved_devices",[]))
                if case.get("knowledge"):
                    checks["knowledge_path"] = data.get("knowledge_status") in ("hit","miss","index_unavailable")
                    if data.get("unverified_explanations"):
                        checks["unverified_label"] = all("未核验" in e["text"] and not e["evidence_ids"] for e in data["unverified_explanations"])
                for fact in data.get("verified_facts",[]):
                    checks["facts_reference_current_sources"] = checks.get("facts_reference_current_sources",True) and bool(fact["evidence_ids"]) and all(any(e["id"]==ref and e["status"] in ("ok","no_data") for e in evidence) for ref in fact["evidence_ids"])
            audit = None
            for path in Path(os.environ["AGENT_AUDIT_DIR"]).glob("audit-*.jsonl"):
                for line in path.read_text(encoding="utf-8").splitlines():
                    row = json.loads(line)
                    if row.get("request_id") == data.get("request_id"):
                        audit = row
            checks["audit_exists"] = audit is not None
            if audit:
                tool_calls = [row for row in audit["calls"] if "tool" in row]
                business = [e for e in data.get("evidence",[]) if e["kind"]=="business"]
                checks["evidence_matches_actual_calls"] = len(tool_calls)==len(business) and all(call["tool"]==e["tool"] and call["status"]==e["status"] for call,e in zip(tool_calls,business))
            passed = all(checks.values())
            rows.append({"question":case["question"],"passed":passed,"checks":checks,"response":data,"actual_calls":audit["calls"] if audit else []})
            (args.output/"responses.json").write_text(json.dumps({"real_model":True,"passed":all(row["passed"] for row in rows),"cases":rows},ensure_ascii=False,indent=2),encoding="utf-8")
            print(index,case["question"],"passed" if passed else "failed", {key:value for key,value in checks.items() if not value}, flush=True)
        print("passed",sum(r["passed"] for r in rows),"/",len(rows))
    return all(row["passed"] for row in rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--create-user",action="store_true")
    parser.add_argument("--case",type=int,nargs="+")
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(main(args)) else 1)
