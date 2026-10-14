"""Acceptance of the actual semantic entry point (legacy loop tests stay separate)."""
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock
import httpx
from agent.context import Context
from agent.llm import ProviderError
from agent.natural import Plan, NaturalAgent, fallback_plan
from agent.orchestrator import Orchestrator
from agent.schemas import ChatResponse, Evidence
from agent.tools import Tools, normalize, route
from test_agent import CFG


def output(value):
    return {"content": json.dumps(value, ensure_ascii=False)}


def plan(**changes):
    return Plan(scope="in_scope", live=True, knowledge=False, intent="status",
                device="ordinal", ordinal=2).model_copy(update=changes).model_dump()


class FixtureBackend:
    def __init__(self):
        self.calls = []
        self.stale = False
        self.fail = set()
        self.incomplete = False
        self.groups = [{"key":"1","device_id":1,"device_code":"INV-1001","count":3},
                       {"key":"2","device_id":2,"device_code":"INV-1002","count":3}]
    def __call__(self, request):
        self.calls.append((request.url.path, dict(request.url.params)))
        path = request.url.path
        if any(word in path for word in self.fail):
            return httpx.Response(503)
        if path.endswith("/devices"):
            rows = [{"id":i,"device_code":f"INV-100{i}","is_simulated":True,"reference_model":"SUN2000-100KTL-M2"} for i in (1,2,3)]
            if request.url.params.get("keyword"):
                rows = [r for r in rows if request.url.params["keyword"] == r["device_code"]]
            data = {"list":rows,"total":len(rows),"page":1,"page_size":100}
        elif path.endswith("/telemetry/latest"):
            now = datetime.now(timezone.utc)
            sample = now-timedelta(seconds=30) if self.stale else now
            data = {"device_id":2,"device_code":"INV-1002","ts_ms":int(sample.timestamp()*1000),
                    "received_at":now.isoformat(),"status":1,"power":12.5,"temperature":39.2}
        elif path.endswith("/statistics"):
            args = dict(request.url.params)
            data = {"groups":self.groups,"total_events":sum(g["count"] for g in self.groups),
                    "complete":not self.incomplete,"group_by":args["group_by"],
                    "start":args["start"],"end":args["end"],"device_id":int(args.get("device_id",0)),
                    "status_filter":args.get("status","")}
        elif path.endswith("/alarms"):
            data = {"list":[],"total":0,"page":1,"page_size":1}
        elif path.endswith("/prediction"):
            data = {"device_id":2,"window_end_ms":int(datetime.now(timezone.utc).timestamp()*1000),
                    "probability":.72,"threshold":.65,"risk_level":"high","stale":False}
        else:
            return httpx.Response(404)
        return httpx.Response(200,json={"code":0,"data":data})


class NaturalTest(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_energy_subject_cannot_be_misclassified_as_daily_life(self):
        outputs=[output(plan(scope="out_of_scope",intent="general",live=False,knowledge=False,device="none",ordinal=None)),output({"facts":[],"explanations":["可能受光、热、水氧影响。"],"suggestions":[]})]
        response, backend, _, trace=await self.run_case("钙钛矿组件衰减机理是什么？",outputs)
        self.assertEqual(response.status,"answered")
        self.assertTrue(response.unverified_explanations)
        self.assertFalse(backend.calls)
        self.assertTrue(any(row.get("scope_validation") for row in trace))
        response, _, provider, _=await self.run_case("讲个笑话，然后看看风机",plan(scope="out_of_scope",intent="general",live=False,knowledge=False))
        self.assertEqual(response.status,"out_of_scope")
        provider.complete.assert_awaited_once()
    async def test_current_alarms_do_not_use_trigger_time_window(self):
        response, backend, _, _ = await self.run_case("INV-1002还有告警吗？",plan(intent="alarms",device="code",code="INV-1002",ordinal=None))
        self.assertEqual(response.status,"answered")
        self.assertEqual(len([p for p,_ in backend.calls if p.endswith("/alarms")]),2)
        self.assertFalse(any(p.endswith("/statistics") for p,_ in backend.calls))
        self.assertIn("未确认且未恢复告警 0 条",response.conclusion.text)
        self.assertIn("无触发时间限制",response.conclusion.text)
        response, _, _, _ = await self.run_case("INV-1002还有告警吗？",ProviderError("model_unreachable"))
        self.assertEqual(response.status,"answered")

    async def test_global_ranking_discards_inherited_device(self):
        response, backend, _, _ = await self.run_case("哪个设备出现的问题最多？",plan(intent="ranking",device="context",ordinal=None),history=[{"role":"assistant","content":json.dumps({"device_context":[{"id":2,"device_code":"INV-1002"}]})}])
        self.assertIn("并列第一",response.conclusion.text)
        query=next(args for p,args in backend.calls if p.endswith("/statistics"))
        self.assertNotIn("device_id",query)

    async def test_code_adjacent_to_chinese_still_resolves_on_model_failure(self):
        response, backend, _, _ = await self.run_case("能看下INV-1002现在的温度吗", ProviderError("model_unreachable"))
        self.assertEqual(response.status,"answered")
        self.assertEqual(response.resolved_devices[0]["id"],2)
        self.assertIn("39.2",response.conclusion.text)
        self.assertIn("/api/v1/devices/2/telemetry/latest",[p for p,_ in backend.calls])

    async def test_transient_failure_retries_only_primary_once(self):
        response, _, provider, _ = await self.run_case("第二台状态如何？",[ProviderError("unavailable"),output(plan())])
        self.assertEqual(response.status,"answered")
        self.assertEqual(provider.complete.await_count,2)

    async def run_case(self, question, result=None, fixture=None, history=None, context=None):
        provider = AsyncMock()
        if isinstance(result, Exception):
            provider.complete.side_effect = result
        elif isinstance(result, list):
            provider.complete.side_effect = result
        else:
            provider.complete.return_value = output(result or plan())
        fixture = fixture or FixtureBackend()
        response = ChatResponse(request_id="r",session_id="s",status="degraded",model="test")
        trace = []
        async with httpx.AsyncClient(transport=httpx.MockTransport(fixture)) as client:
            response = await Orchestrator(CFG,provider,Tools(CFG,client)).run(question,history or [],"jwt",response,trace,context=context)
        return response, fixture, provider, trace

    async def test_second_device_all_mandatory_phrasings_and_typo(self):
        questions = json.loads(Path(__file__).with_name("natural-cases.json").read_text(encoding="utf-8"))[:6]
        for case in questions:
            with self.subTest(question=case["question"]):
                response, backend, provider, trace = await self.run_case(case["question"])
                self.assertEqual(response.status,"answered")
                self.assertIn("INV-1002",response.conclusion.text)
                self.assertIn("发电中",response.conclusion.text)
                self.assertIn("/api/v1/devices/2/telemetry/latest",[p for p,_ in backend.calls])
                self.assertTrue(any("ID升序" in n for n in response.notices))
                self.assertEqual(response.source_coverage_percent,100)
                provider.complete.assert_awaited_once()
                self.assertTrue(all(row.get("forced") for row in trace if "tool" in row))

    async def test_semantics_beats_keywords_but_live_evidence_is_mandatory(self):
        response, backend, _, _ = await self.run_case("劳驾瞅瞅排老二的还行不",plan())
        self.assertIn("INV-1002",response.conclusion.text)
        response, backend, _, _ = await self.run_case("第二台设备状态如何？",plan(live=False,intent="general",knowledge=False,device="none"))
        self.assertIn("/api/v1/devices/2/telemetry/latest",[p for p,_ in backend.calls])

    async def test_references_are_revalidated_each_turn(self):
        first, _, _, _ = await self.run_case("第二台设备状态如何？")
        history = [{"role":"assistant","content":json.dumps({"answer":"旧回答：故障状态", "device_context":first.resolved_devices})}]
        second, backend, _, _ = await self.run_case("它现在怎么样？",plan(device="context",ordinal=None),history=history)
        self.assertIn("发电中",second.conclusion.text)
        self.assertNotIn("故障状态",second.conclusion.text)
        self.assertEqual([p for p,_ in backend.calls].count("/api/v1/devices/2/telemetry/latest"),1)
        third, backend, _, _ = await self.run_case("它现在怎么样？",plan(device="context",ordinal=None))
        self.assertEqual(third.status,"unable_to_determine")
        self.assertFalse(backend.calls)

    async def test_stale_sample_even_if_recently_received_and_partial_failure(self):
        fixture = FixtureBackend(); fixture.stale = True
        response, _, _, _ = await self.run_case("第二台有没有异常？",fixture=fixture)
        self.assertNotIn("发电中",response.conclusion.text)
        self.assertTrue(any("状态未知" in line for line in response.limitations))
        self.assertTrue(any("未确认告警 0" in f.text for f in response.verified_facts))
        fixture.fail = {"/alarms"}
        response, _, _, _ = await self.run_case("第二台状态如何？",fixture=fixture)
        self.assertEqual(response.status,"unable_to_determine")
        self.assertIsNone(response.conclusion)

    async def test_rank_tie_zero_incomplete_failure_and_window(self):
        fixture = FixtureBackend()
        response, backend, _, _ = await self.run_case("哪个设备出现的问题最多？",plan(intent="ranking",device="none",ordinal=None),fixture)
        self.assertIn("并列第一",response.conclusion.text)
        self.assertIn("INV-1001、INV-1002",response.conclusion.text)
        self.assertIn("包含已恢复和未恢复",response.conclusion.text)
        args = backend.calls[0][1]
        self.assertEqual((datetime.fromisoformat(args["end"])-datetime.fromisoformat(args["start"])).days,30)
        self.assertFalse(any(p.endswith("/alarms") for p,_ in backend.calls))
        fixture.groups = []
        response, _, _, _ = await self.run_case("哪个设备出现的问题最多？",plan(intent="ranking",device="none",ordinal=None),fixture)
        self.assertIn("该范围内无告警记录",response.conclusion.text)
        for incomplete, fail in [(True,set()),(False,{"statistics"})]:
            fixture.incomplete, fixture.fail = incomplete,fail
            response, _, _, _ = await self.run_case("哪个设备出现的问题最多？",plan(intent="ranking",device="none",ordinal=None),fixture)
            self.assertEqual(response.status,"unable_to_determine")
            self.assertIsNone(response.conclusion)

    async def test_inconsistent_semantic_flags_cannot_disable_aggregate(self):
        response, backend, _, _ = await self.run_case("近7天哪台报警次数排第一",plan(live=False,intent="ranking",device="all",ordinal=None,days=7))
        self.assertEqual(response.status,"answered")
        self.assertTrue(any(path.endswith("/statistics") for path,_ in backend.calls))
        args = backend.calls[0][1]
        self.assertEqual((datetime.fromisoformat(args["end"])-datetime.fromisoformat(args["start"])).days,7)

    async def test_forged_quote_cannot_inflate_coverage(self):
        chunk = {"id":"K"+"c"*20,"content":"确认只表示人员知悉。","heading":"告警定义"}
        knowledge = AsyncMock(); knowledge.search=lambda q:("hit",[chunk]); knowledge.version="v"
        search, misses = AsyncMock(), AsyncMock(); search.run.return_value=("not_eligible",[])
        outputs = [output(plan(live=False,knowledge=True,intent="general",device="none",ordinal=None)),
                   output({"relevant_ids":[chunk["id"]]}),
                   output({"facts":[{"text":"恢复可能意味着指标回到正常范围。","evidence_ids":["E1"]}],"suggestions":[],"explanations":[]}),
                   output({"checks":[{"index":0,"supported":True,"evidence_id":"E1","quote":"资料中根本没有的文本"}]})]
        response, _, _, _ = await self.run_case("恢复是什么意思？",outputs,context=Context(knowledge,search,misses))
        self.assertEqual(response.sourced_claims,0)
        self.assertEqual(response.source_coverage_percent,0)
        self.assertEqual(response.knowledge_status,"miss")
        misses.write.assert_awaited_once()

    async def test_unsourced_current_state_is_rejected_even_as_explanation(self):
        outputs = [output(plan(live=False,knowledge=True,intent="general",device="none",ordinal=None)),
                   output({"facts":[],"explanations":["当前设备状态正常，本平台温度阈值为99。"],"suggestions":[]})]
        response, _, _, _ = await self.run_case("温度阈值是什么？",outputs)
        self.assertEqual(response.status,"unable_to_determine")
        self.assertIsNone(response.conclusion)
        self.assertFalse(response.unverified_explanations)

    async def test_explicit_ordinal_discards_invented_code_and_checks_order(self):
        response, _, _, _ = await self.run_case("第二台设备状态如何？",plan(code="INV-1003",device_id=3))
        self.assertEqual(response.resolved_devices[0]["device_code"],"INV-1002")
        backend = FixtureBackend()
        original = backend.__call__
        def unsorted(request):
            reply = original(request)
            if request.url.path.endswith("/devices"):
                data = reply.json(); data["data"]["list"].reverse()
                return httpx.Response(200,json=data)
            return reply
        response, _, _, _ = await self.run_case("第二台设备状态如何？",fixture=unsorted)
        self.assertEqual(response.status,"unable_to_determine")
        self.assertIsNone(response.conclusion)

    def test_manual_sources_require_matching_reference_model(self):
        agent = NaturalAgent(CFG,AsyncMock(),AsyncMock())
        response = ChatResponse(request_id="r",session_id="s",status="degraded",model="test",
            resolved_devices=[{"id":2,"reference_model":"OTHER-MODEL"}])
        self.assertFalse(agent.manual_applicable({"source_kind":"manual_summary","content":"SUN2000-100KTL-M2 手册"},response))

    async def test_model_failure_known_business_and_open_question(self):
        for question in ("第二台设备状态如何？","帮我看看第二台现在怎么样","第二台有没有异常？","哪个设备出现的问题最多？"):
            response, backend, _, trace = await self.run_case(question,ProviderError("model_unreachable"))
            self.assertEqual(response.status,"answered")
            self.assertTrue(response.verified_facts)
            self.assertEqual(response.unverified_explanations,[])
            self.assertTrue(backend.calls)
        response, backend, _, _ = await self.run_case("风机覆冰的原理是什么？",ProviderError("model_unreachable"))
        self.assertEqual(response.status,"degraded")
        self.assertFalse(backend.calls)

    async def test_out_of_scope_no_business_no_knowledge(self):
        for question in ("怎么做番茄炒蛋？","推荐爱情电影","讲个笑话，然后看第二台"):
            response, backend, _, _ = await self.run_case(question,plan(scope="out_of_scope",live=False,knowledge=False,intent="general"))
            self.assertEqual(response.status,"out_of_scope")
            self.assertIn("新能源",response.conclusion.text)
            self.assertEqual(response.total_claims,0)
            self.assertFalse(backend.calls)
            self.assertFalse(response.evidence)

    async def test_out_of_library_model_knowledge_is_individually_unverified(self):
        knowledge = AsyncMock(); knowledge.search = lambda query:("miss",[]); knowledge.version="v"
        search, misses = AsyncMock(), AsyncMock(); search.run.return_value=("not_configured",[])
        outputs = [output(plan(live=False,knowledge=True,intent="general",device="none",ordinal=None)),
                   output({"facts":[],"explanations":["覆冰可能改变叶片气动外形并降低发电效率","不均匀覆冰可能使转子振动增加"],"suggestions":[]})]
        response, backend, _, _ = await self.run_case("风机覆冰为什么影响发电？",outputs,context=Context(knowledge,search,misses))
        self.assertEqual(response.status,"answered")
        self.assertEqual(response.source_coverage_percent,0)
        self.assertEqual(len(response.unverified_explanations),2)
        self.assertTrue(all("未核验" in c.text and not c.evidence_ids for c in response.unverified_explanations))
        self.assertEqual(response.miss_record_status,"recorded")
        self.assertFalse(backend.calls)

    async def test_source_coverage_requires_entailment_not_citation(self):
        chunk = {"id":"K"+"a"*20,"content":"确认表示值班人员已知悉告警，不等于恢复。","heading":"告警语义","source_kind":"project"}
        knowledge = AsyncMock(); knowledge.search=lambda q:("hit",[chunk]); knowledge.version="v"
        search, misses = AsyncMock(), AsyncMock()
        outputs = [output(plan(live=False,knowledge=True,intent="general",device="none",ordinal=None)),
                   output({"relevant_ids":[chunk["id"]]}),
                   output({"facts":[{"text":"确认表示人员已知悉告警，不等于恢复。","evidence_ids":["E1"]},
                                    {"text":"复位可以消除所有故障。","evidence_ids":["E1"]}],"explanations":[],"suggestions":[]}),
                   output({"checks":[{"index":0,"supported":True,"evidence_id":"E1","quote":chunk["content"]},
                                      {"index":1,"supported":False,"evidence_id":"E1","quote":""}]})]
        response, _, _, _ = await self.run_case("确认和恢复有什么区别？",outputs,context=Context(knowledge,search,misses))
        self.assertEqual(response.sourced_claims,1)
        self.assertEqual(response.total_claims,2)
        self.assertEqual(response.source_coverage_percent,50)
        self.assertEqual(len(response.unverified_explanations),1)

    async def test_forged_quote_and_irrelevant_page_cannot_be_verified(self):
        chunk = {"id":"K"+"b"*20,"content":"页面支持设备列表。","heading":"设备页面"}
        knowledge = AsyncMock(); knowledge.search=lambda q:("hit",[chunk]); knowledge.version="v"
        search, misses = AsyncMock(), AsyncMock(); search.run.return_value=("not_eligible",[])
        outputs = [output(plan(live=False,knowledge=True,intent="general",device="none",ordinal=None)),
                   output({"relevant_ids":[]}),
                   output({"facts":[],"explanations":["可能需要检查散热资料"],"suggestions":[]})]
        response, _, _, _ = await self.run_case("风机过热的原因？",outputs,context=Context(knowledge,search,misses))
        self.assertEqual(response.knowledge_status,"miss")
        self.assertFalse(response.evidence)
        misses.write.assert_awaited_once()

    async def test_live_manual_mix_keeps_business_when_model_fails(self):
        outputs = [output(plan(knowledge=True,knowledge_query="指示灯的含义")), ProviderError("model_timeout")]
        response, backend, _, _ = await self.run_case("第二台状态如何，手册指示灯什么意思？",outputs)
        self.assertEqual(response.status,"answered")
        self.assertIn("发电中",response.conclusion.text)
        self.assertTrue(any("主模型" in x for x in response.limitations))
        self.assertTrue(any("telemetry/latest" in path for path,_ in backend.calls))

    def test_statistics_tool_allowlist_and_incomplete_results(self):
        args = {"group_by":"device","start":"2026-09-09T00:00:00Z","end":"2026-10-09T00:00:00Z"}
        self.assertEqual(route("get_alarm_statistics",args)[0],"/api/v1/alarms/statistics")
        for changes in ({"group_by":"device;DROP"},{"sql":"select"},{"start":"2026-09-09"},{"end":args["start"]}):
            with self.assertRaises(ValueError): route("get_alarm_statistics",{**args,**changes})
        with self.assertRaises(ValueError): normalize("get_alarm_statistics",{**args,"groups":[],"total_events":0,"complete":False})

    def test_plan_rejects_extra_fields_loose_types_and_unknown_tools(self):
        for changes in ({"sql":"select"},{"live":"true"},{"device_id":True},{"intent":"control"},{"days":0}):
            with self.assertRaises(ValueError): Plan.model_validate({**plan(),**changes})


if __name__ == "__main__": unittest.main()
