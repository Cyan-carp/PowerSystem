"""Supervisor delegation. No platform GET execution exists in this adapter."""
import asyncio
import json
import time
from datetime import datetime, timezone
from uuid import UUID, uuid4
from pydantic import Field
from fastapi import HTTPException
from .tools import Tools, Args
from .ma_security import digest, sign, verify

TERMINAL = {"succeeded", "failed", "cancelled", "timeout"}


class Dispatch(Args):
    intent: str = Field(min_length=1, max_length=4000)
    target_description: str | None = Field(default=None, max_length=2000)
    operation: __import__('typing').Literal['read_only','restart_drill_container'] = 'read_only'


class GetTask(Args):
    task_id: UUID


def envelope_signature(value, key):
    now = int(time.time())
    return sign(dict(aud="ma-envelope", kid="ma-v1", iat=now, exp=now+450, hash=digest(value)), key)


class Ops:
    def __init__(self, cfg, client, actor_token, jwt, sid, user, question=""):
        self.cfg, self.client, self.actor_token, self.jwt = cfg, client, actor_token, jwt
        self.sid, self.user, self.question = str(sid), user, question
        self.actor = verify(actor_token, cfg.ma_actor_key, "ma-ops") if cfg.ma_actor_key else None
        if self.actor and (self.actor["user_id"] != user or self.actor["session_id"] != self.sid): raise HTTPException(403, "context_mismatch")

    async def request(self, method, path, body=None, signed=None):
        cfg = self.cfg
        if not cfg.ops_url or not cfg.ops_token or not self.actor: raise HTTPException(503, "ops_unconfigured")
        headers = {"X-PowerSystem-Token": cfg.ops_token, "Authorization": "Bearer " + self.jwt, "X-MA-Actor": self.actor_token}
        if signed is not None: headers["X-MA-Envelope"] = envelope_signature(signed, cfg.ma_envelope_key)
        try:
            r = await self.client.request(method, cfg.ops_url + path, json=body, headers=headers, timeout=35 if path.endswith('/model-summary') else cfg.tool_timeout)
            if r.status_code not in (200,202):
                raise HTTPException(r.status_code if r.status_code in (400,401,403,404,409,429,503) else 503, "ops_"+str(r.status_code), headers={"Retry-After": r.headers.get("Retry-After", "2")} if r.status_code == 429 else None)
            return r.json()
        except (TimeoutError, OSError): raise HTTPException(503, "ops_unavailable") from None
        except __import__('httpx').HTTPError: raise HTTPException(503, "ops_unavailable") from None

    async def dispatch(self, intent, target=None, key=None, parent=None, verification=False, operation='read_only'):
        actor = self.actor
        if actor is None: raise HTTPException(503, "ops_unconfigured")
        if operation=='restart_drill_container':
            import re
            if actor['role']!='admin': raise HTTPException(403,'admin_required')
            if verification or not re.search(r'重启|\brestart\b',self.question,re.I) or re.search(r'不要|禁止|别重启|不重启|如何|怎么|解释|如果|假如|how\s+(?:to|do)|do\s+not|don.t',self.question,re.I): raise HTTPException(400,'invalid_write_intent')
        envelope = dict(protocol="ma/v1", task_id=str(uuid4()), trace_id=actor["trace_id"], idempotency_key=str(key or uuid4()), session_id=self.sid,
                        parent_turn_id=parent, actor={"user_id": self.user, "role": actor["role"]}, intent=intent,
                        user_request=self.question, targets=[{"description": target, "asset_id": None}] if target else [],
                        constraints={"max_risk_class": "R2" if operation=='restart_drill_container' else "R0", "timeout_sec": 300}, created_at=datetime.now(timezone.utc).isoformat(),operation=operation)
        # Reuse exactly one envelope and key for a transport retry after a lost acceptance response.
        for attempt in range(2):
            try: return await self.request("POST", "/internal/ma/v1/verifications" if verification else "/internal/ma/v1/tasks", envelope, envelope)
            except HTTPException as e:
                if e.status_code != 503 or attempt: raise
                await asyncio.sleep(.2)

    async def get(self, tid):
        return await self.request("GET", "/internal/ma/v1/tasks/"+str(UUID(str(tid))))

    async def wait(self, task, seconds):
        until = time.monotonic()+max(0, seconds)
        while task["status"] not in TERMINAL | {'awaiting_approval'} and time.monotonic() < until:
            await asyncio.sleep(min(.4, max(0, until-time.monotonic())))
            remaining = until-time.monotonic()
            if remaining <= 0: break
            try:
                async with asyncio.timeout(remaining): task = await self.get(task["task_id"])
            except TimeoutError: break
        return task

    async def bind(self, tid, turn):
        body = {"session_id": self.sid, "parent_turn_id": turn}
        return await self.request("POST", "/internal/ma/v1/tasks/"+tid+"/bind-turn", body, {"task_id": tid, **body})

    async def guard(self, op, action):
        return await self.request("POST", "/internal/ma/v1/sessions/"+self.sid+"/guard", {"operation_id": str(op), "action": action})


class SupervisorTools:
    schemas = [s for s in Tools.schemas if s["function"]["name"] in ("search_knowledge", "web_search")] + [
        {"type": "function", "function": {"name": "dispatch_ops_task", "description": "委派平台查数或本项目基础设施只读诊断，默认operation=read_only。用户明确要求重启本机隔离演练容器时可提出operation=restart_drill_container，由程序检查当前admin并生成审批卡；不直接执行，不审批，不用于生产容器。服务器慢或帮我处理先只读诊断，不推导重启授权。目标不明确先澄清，纯技术解释不创建任务，一次请求只委派一个完整任务。", "parameters": Dispatch.model_json_schema()}},
        {"type": "function", "function": {"name": "get_ops_task", "description": "读取本人运维任务结果用于解释；历史报告不是当前状态。", "parameters": GetTask.model_json_schema()}}]

    def __init__(self, original, ops, deadline):
        self.original, self.ops, self.deadline, self.dispatched = original, ops, deadline, None
        self.egress_blocked = False
        self.egress_permits=[]
        if getattr(getattr(ops,'cfg',None),'acceptance_drill_only',False):
            self.schemas=[s for s in type(self).schemas if s['function']['name'] in ('dispatch_ops_task','get_ops_task')]

    async def execute(self, name, arguments, jwt, result):
        if getattr(getattr(self.ops,'cfg',None),'acceptance_drill_only',False) and name not in ('dispatch_ops_task','get_ops_task'):
            return {'error':'本次隔离验收仅允许演练委派与本人任务读取'}
        if name in ("search_knowledge", "web_search"):
            return await self.original.execute(name, arguments, jwt, result)
        trace = {"name": name, "arguments": {}, "status": "error", "collected_at": datetime.now(timezone.utc).isoformat()}
        security_required = False
        result["tool_calls"].append(trace)
        try:
            if name == "dispatch_ops_task":
                args = Dispatch.model_validate(json.loads(arguments))
                trace["arguments"] = args.model_dump(exclude_none=True)
                if self.dispatched is None:
                    self.dispatched = await self.ops.dispatch(args.intent, args.target_description,operation=args.operation)
                    result["task_cards"] = [self.dispatched]
                    result["response_mode"] = "task_dispatched"
                task = await self.ops.wait(self.dispatched, min(15, max(0, self.deadline-time.monotonic()-5)))
                self.dispatched = task; result["task_cards"] = [task]
                # Actual delegated calls only: queued tasks never become queried evidence.
                result["tool_calls"].extend(c for c in task.get("tool_calls", []) if c not in result["tool_calls"])
                result["sources"].extend(s for s in task.get("sources", []) if s not in result["sources"])
            elif name == "get_ops_task":
                args = GetTask.model_validate(json.loads(arguments)); trace["arguments"] = {"task_id": str(args.task_id)}
                task = await self.ops.get(args.task_id)
                result.setdefault("task_cards", []).append({**task, "historical": True})
            else: raise ValueError("unknown_supervisor_tool")
            trace.update(status="ok", result={"task_id": task["task_id"], "status": task["status"]})
            if task.get('security_reviews'):
                security_required = True
                # Full report remains in task_cards. No report prose or private material reaches a model.
                if task['status'] not in TERMINAL:
                    return {'task_id':task['task_id'],'status':task['status']}
                if self.ops.cfg.llm_url != 'https://hgapi.dieqiyun.top/v1' or self.ops.cfg.model != 'deepseek-v4.1-flash':
                    raise ValueError('unlicensed_supervisor_destination')
                reviewed=await self.ops.request('POST','/internal/ma/v1/tasks/'+task['task_id']+'/model-summary',{})
                if set(reviewed)!={'payload','payload_hash','review_id'} or digest(reviewed['payload'])!=reviewed['payload_hash']:
                    raise ValueError('security_egress_binding_mismatch')
                self.egress_permits.append((task['task_id'],reviewed))
                return reviewed['payload']
            return task
        except Exception as e:
            if security_required: self.egress_blocked = True
            reason = "运维服务不可用或参数无效" if not isinstance(e, HTTPException) else e.detail
            trace["error"] = reason; result["warnings"].append(reason)
            return {"error": reason}

    async def check_egress(self):
        for tid,reviewed in self.egress_permits:
            if self.ops.cfg.llm_url!='https://hgapi.dieqiyun.top/v1' or self.ops.cfg.model!='deepseek-v4.1-flash' or digest(reviewed['payload'])!=reviewed['payload_hash']:
                raise ValueError('security_egress_binding_mismatch')
            value=await self.ops.request('POST','/internal/ma/v1/tasks/'+tid+'/model-summary/check',{'review_id':reviewed['review_id'],'payload_hash':reviewed['payload_hash']})
            if value!={'permitted':True}:raise ValueError('security_review_stale')
