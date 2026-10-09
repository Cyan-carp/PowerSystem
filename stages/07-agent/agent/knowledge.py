"""Versioned, closed-world Markdown retrieval. No request-controlled file reads."""
import hashlib
import json
import re
from pathlib import Path

ALIASES = {
    "离线": ["掉线", "没消息", "不在线", "断线", "通信中断"],
    "告警确认": ["确认告警", "签收", "已知悉", "确认按钮"],
    "已恢复": ["恢复告警", "恢复正常"],
    "故障预测": ["预测结果", "风险概率", "预测依据", "概率", "特征贡献"],
    "结果过期": ["预测过期", "不新鲜", "陈旧"],
    "历史曲线": ["曲线", "趋势", "历史数据", "波形"],
    "设备管理": ["设备列表", "分组筛选", "设备分组"],
    "实时连接": ["连接断开", "连接中断", "实时更新", "自动重连"],
    "发电量": ["累计电量", "当日电量", "kwh", "电量"],
    "当前功率": ["功率单位", "kw", "功率"],
    "飞书": ["群消息", "机器人通知", "值班群"],
    "值班问答": ["问答", "智能助手", "聊天"],
    "知识库": ["文档检索", "联网搜索", "联网", "未命中"],
    "sun2000-100ktl-m2": ["inv-1001", "inv-1002", "inv-1003", "仿真参考型号"],
}
STOP = {"如何", "怎么", "什么", "为什么", "是否", "可以", "应该", "这个", "一下", "设备", "平台", "问题", "查询", "现在", "当前", "请问", "查看", "功能", "使用", "说明", "帮助", "检查"}


def normalize_query(text):
    value = re.sub(r"\s+", " ", text.lower()).strip()
    for canonical, aliases in ALIASES.items():
        if canonical in value or any(alias in value for alias in aliases):
            value += " " + canonical
    return value


def tokens(text):
    value = normalize_query(text)
    result = set(re.findall(r"[a-z][a-z0-9_-]{1,30}", value))
    for word in re.findall(r"[\u4e00-\u9fff]+", value):
        result.update(word[i:i+2] for i in range(len(word)-1))
    return result - STOP


class Knowledge:
    def __init__(self, index_path):
        self.version, self.chunks, self.available = "", {}, False
        try:
            raw = Path(index_path).read_bytes()
            if len(raw) > 4_000_000:
                raise ValueError("oversized index")
            index = json.loads(raw)
            payload = json.dumps(index["chunks"], ensure_ascii=False, sort_keys=True).encode()
            if index["version"] != hashlib.sha256(payload).hexdigest():
                raise ValueError("index hash mismatch")
            for chunk in index["chunks"]:
                if not re.fullmatch(r"K[a-f0-9]{20}", chunk["id"]) or len(chunk["content"]) > 12000:
                    raise ValueError("invalid chunk")
                self.chunks[chunk["id"]] = chunk
            self.version, self.available = index["version"], True
        except (OSError, ValueError, KeyError, TypeError):
            self.chunks = {}

    def search(self, query, limit=5):
        if not self.available:
            return "index_unavailable", []
        wanted = tokens(query)
        concepts = {word for word in ALIASES if word in normalize_query(query)}
        ranked = []
        for chunk in self.chunks.values():
            title = tokens(chunk["heading"])
            body = tokens(chunk["content"])
            matched = wanted & (title | body)
            coverage = len(matched) / max(1, len(wanted))
            concept_match = any(word in normalize_query(chunk["heading"] + " " + chunk["content"]) for word in concepts)
            if (len(matched) < 2 or coverage < .3) and not concept_match:
                continue
            score = coverage + 2 * len(wanted & title) / max(1, len(wanted))
            score += .15 if chunk["authority"] == "current" else 0
            score += .4 if concept_match else 0
            if chunk.get("source_kind") == "manual_summary" and re.search(r"sun2000|100ktl|华为|手册|型号|afci|指示灯|故障码", query, re.I):
                score += .6
            ranked.append((score, chunk))
        ranked.sort(key=lambda row: (-row[0], row[1]["id"]))
        return ("hit" if ranked else "miss"), [row[1] for row in ranked[:limit]]

    def get(self, chunk_id):
        return self.chunks.get(chunk_id)
