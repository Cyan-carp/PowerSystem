"""Deterministic knowledge-first context and controlled external fallback."""
import asyncio
import re
import json
from .schemas import Evidence
from .tools import now
from .search import public_query, STATUS_LABELS


def needs_knowledge(message):
    # A question without an explicit request for live data is a knowledge
    # question too ("告警级别有哪些", "历史曲线最长能看多久", etc.).
    if re.search(r"如何|怎么|为什么|什么意思|什么是|说明|操作|步骤|排查|帮助|引导|知识库|文档|手册|型号|规格|指示灯|故障码|华为|单位|区别|确认|过期|没消息|掉线|查看|支持", message):
        return True
    return not needs_business(message) and bool(re.search(r"哪些|多久|是否|吗|？|\?", message))


def needs_business(message):
    if re.search(r"(?:设备|告警)\s*#?\s*\d+|列出|查询所有|查询设备|查询告警|多少台|多少条", message):
        return True
    if re.search(r"当前|现在|目前|此刻|今天|最近|正在", message):
        return bool(re.search(r"告警|设备|预测|遥测|功率|电压|电流|温度|总览|状态", message))
    if re.search(r"区别|什么意思|什么是|单位", message):
        return False
    return bool(re.search(r"实时", message) and re.search(r"有哪些|多少|查询|列出|状态|数据", message))


def required_business_tools(message, force=False):
    """Tool categories needed to make a live claim; unrelated tools cannot satisfy them."""
    if not force and not needs_business(message):
        return ()
    required = []
    if "告警" in message:
        required.append(frozenset(("get_alarm_detail",)) if re.search(r"告警\s*#?\s*\d+", message)
                        else frozenset(("list_alarms",)))
    if "预测" in message or "风险概率" in message:
        required.append(frozenset(("get_prediction",)))
    if re.search(r"遥测|电压|电流|温度|功率|曲线", message) and "预测" not in message:
        required.append(frozenset(("get_telemetry", "get_dashboard_summary")))
    if "设备" in message and not required:
        required.append(frozenset(("list_devices", "get_telemetry", "get_prediction", "get_dashboard_summary")))
    return tuple(required) or (frozenset(("list_devices", "list_alarms", "get_alarm_detail",
                                       "get_prediction", "get_telemetry", "get_dashboard_summary")),)


class Context:
    def __init__(self, knowledge, search, misses):
        self.knowledge, self.search, self.misses = knowledge, search, misses

    async def prepare(self, message, response, jwt):
        if not needs_knowledge(message):
            return
        status, chunks = self.knowledge.search(message)
        response.knowledge_status = status
        for chunk in chunks:
            item = Evidence(id=f"E{len(response.evidence)+1}", tool="search_knowledge", status="ok",
                kind="document", source="/api/v1/agent/knowledge/" + chunk["id"], collected_at=now(),
                document_version=self.knowledge.version, chapter=chunk["heading"], data=chunk)
            if len(json.dumps([e.model_dump() for e in [*response.evidence, item]], ensure_ascii=False).encode()) <= 50000:
                response.evidence.append(item)
        if status == "hit":
            return
        if status == "index_unavailable":
            response.notices.append("知识库索引不可用，不能将本次失败判定为文档缺失。")
        else:
            response.notices.append("本地知识库未找到足够依据。")
        query = public_query(message) if status == "miss" else ""
        try:
            response.web_status, results = await self.search.run(query) if query else ("not_eligible", [])
        except asyncio.CancelledError:
            response.web_status = "cancelled"
            task = asyncio.create_task(self.record(message, response, jwt, query, status, []))
            # Finish the confirmed miss before request audit and lock release.
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
            task.result()
            raise
        for row in results:
            response.evidence.append(Evidence(id=f"E{len(response.evidence)+1}", tool="web_search", status="ok",
                kind="web", source=row["url"], url=row["url"], collected_at=now(), data=row))
        if results:
            response.notices.append("本地知识库未找到足够依据，以下联网来源仅供参考，不能证明本项目配置或当前设备状态。")
        elif query:
            response.notices.append("联网检索未取得可靠依据（" + STATUS_LABELS.get(response.web_status, "搜索响应异常") + "）。")
        else:
            response.notices.append("项目私有状态或缺少明确公开检索主题，不使用联网结果替代项目依据。")
        await self.record(message, response, jwt, query, status, results)

    async def record(self, message, response, jwt, query, status, results):
        try:
            await self.misses.write({"request_id": response.request_id, "question": message,
                "normalized_question": re.sub(r"\s+", " ", message).strip().lower(), "query": query,
                "reason": status, "knowledge_version": self.knowledge.version,
                "web_status": response.web_status, "sources": [row["url"] for row in results],
                "source_details": results, "knowledge_followup": "pending"}, secrets=(jwt,))
            response.miss_record_status = "recorded"
        except OSError:
            response.miss_record_status = "write_failed"
            response.notices.append("知识缺口记录写入失败，已记录审计状态，请联系管理员检查持久目录。")
