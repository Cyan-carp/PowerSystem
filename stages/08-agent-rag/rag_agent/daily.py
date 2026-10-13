"""BND-P05 deterministic boundary: no models, embeddings or I/O; unknowns pass."""
import re
import unicodedata
from dataclasses import dataclass

RULE_VERSION = "bnd-m2-v1"
SCOPE_REPLY = "生活与闲聊问题暂不提供内容回答。你可以询问技术、新能源设备或平台相关问题。"
MIXED_REPLY = "这条消息同时包含技术或平台问题与生活问题，请分开发送。我可以回答其中的技术、新能源设备或平台问题。"
PRESETS = {
    "greeting": "你好！可以向我提问技术、新能源设备或平台相关问题。",
    "thanks": "不客气，欢迎继续提问技术或平台相关问题。",
    "farewell": "再见，需要技术或平台帮助时可以继续提问。",
}

def normalize(text):
    return re.sub(r"[\u200b-\u200f\ufeff]", "", unicodedata.normalize("NFKC", text)).lower().strip()

SOCIAL = tuple((category, re.compile(pattern)) for category, pattern in (
    ("thanks", r"(?:(?:谢谢(?:你|您|啦|了)?|感谢(?:你|您)?|多谢|辛苦了)|(?:thanks?(?:\s+you)?|thank\s+you)(?:\s+very\s+much)?)[\s!！。.,，~～]*"),
    ("farewell", r"(?:再见|拜拜|回头见|bye(?:\s+bye)?|goodbye)[\s!！。.,，~～]*"),
    ("greeting", r"(?:你好(?:啊|呀|哇)?|您好|嗨|哈喽|早上好|下午好|晚上好|早安|晚安|hi|hello|hey|good\s+(?:morning|afternoon|evening))[\s!！。.,，~～]*"),
))
TECH = re.compile(
    r"(?<![a-z0-9_])(?:tcp|udp|http[s]?|redis|docker|python|java(?:script)?|typescript|vue|react|go|golang|sql|mqtt|linux|windows|api|rag|llm|git|cpu|gpu)(?![a-z0-9_])"
    r"|平台|设备|逆变器|光伏|新能源|电站|场站|遥测|告警|运维|巡检|预测|故障|部署|数据库|服务器|编程|代码|算法|字符串|数据集|机器学习|向量|电压|电流|功率|网络协议"
)
QUOTED = re.compile(r"```[\s\S]*?```|`[^`]*`|“[^”]*”|‘[^’]*’|\"[^\"]*\"|'[^']*'")
TECH_APPLICATION = re.compile(r"代码|程序|脚本|算法|字符串|数据集|数据库|接口|网页|网站|\b(?:code|script|algorithm|dataset|string|database)\b")
SPLIT = re.compile(r"[，,；;。!?！？\n]+|(?:然后|另外|顺便|以及)|(?:再|和)(?=推荐|告诉|帮|给|说|写|解释)|\b(?:and\s+then|also)\b")
FOLLOW = re.compile(r"(?:请|麻烦|帮我)?(?:继续(?:说|讲|解释|介绍)?|再说(?:说|一下)?|展开(?:说说|一下)?|详细(?:说说|一点|点)?|具体(?:步骤|怎么做|做法)(?:呢|是什么)?|它(?:呢|怎么做)|那(?:这个|个)?呢|还有呢|continue|tell\s+me\s+more|more\s+details|what\s+about\s+it)[\s?？!！。.,，]*")

@dataclass(frozen=True)
class Rule:
    id: str
    category: str
    pattern: re.Pattern

RULES = tuple(Rule(rid, category, re.compile(pattern)) for rid, category, pattern in (
    ("D01", "food", r"西红柿炒蛋|番茄炒蛋|炒鸡?蛋|食谱|菜谱|(?:早餐|午餐|晚餐|晚饭|午饭|早饭|夜宵).{0,12}(?:吃|做|推荐)|(?:吃|做|推荐).{0,12}(?:早餐|午餐|晚餐|晚饭|午饭|早饭|夜宵)|(?:怎么|如何).{0,8}(?:做饭|做菜|炒菜|煮饭)|(?:推荐|教我).{0,8}(?:美食|餐厅|饭店)|\b(?:recipe|recipes|dinner|breakfast|lunch|cook(?:ing)?)\b"),
    ("D02", "travel", r"旅游攻略|旅行攻略|(?:推荐|安排|规划|制定).{0,12}(?:旅游|旅行|度假|景点|行程)|(?:旅游|旅行|度假|景点).{0,12}(?:推荐|攻略|去哪|怎么玩)|\b(?:vacation|travel\s+itinerary|tourist\s+attractions)\b"),
    ("D03", "entertainment", r"(?:推荐|讲|说|写|来).{0,10}(?:电影|电视剧|综艺|笑话|段子|故事|情诗|情书)|(?:电影|电视剧|综艺).{0,10}(?:好看|推荐)|陪我(?:聊|玩)|聊(?:聊天|天吧|会天)|我(?:好|很|太)?无聊|\b(?:tell\s+(?:me\s+)?(?:a\s+)?(?:joke|story)|recommend.{0,20}(?:movie|film)|chat\s+with\s+me)\b"),
    ("D04", "personal", r"谈恋爱|失恋|追女生|追男生|怎么表白|如何表白|(?:穿搭|化妆|减肥|健身|护肤).{0,12}(?:建议|推荐|怎么|如何|计划)|(?:推荐|制定|安排).{0,12}(?:穿搭|健身|减肥|护肤)|\b(?:dating\s+advice|relationship\s+advice|workout\s+plan)\b"),
    ("D05", "weather", r"(?:今天|明天|后天|周末).{0,8}(?:天气|下雨)|天气(?:如何|怎么样|预报)|\b(?:weather\s+(?:today|tomorrow|forecast)|(?:today'?s|tomorrow'?s)\s+weather)\b"),
))

@dataclass(frozen=True)
class Decision:
    action: str = "allow"
    reason: str = "uncertain"
    rule_id: str = ""

    def metadata(self):
        return {"action": self.action, "reason": self.reason, "rule_id": self.rule_id, "rule_version": RULE_VERSION}

    def reply(self):
        if self.action == "preset": return PRESETS[self.reason]
        return MIXED_REPLY if self.reason == "mixed" else SCOPE_REPLY

def current_decision(question):
    text = normalize(question)
    for category, pattern in SOCIAL:
        if pattern.fullmatch(text): return Decision("preset", category, "S-" + category)
    social_parts = [part.strip() for part in SPLIT.split(text) if part.strip()]
    social_kinds = [next((kind for kind, pattern in SOCIAL if pattern.fullmatch(part)), None) for part in social_parts]
    if social_kinds and all(social_kinds):
        category = next(kind for kind in ('thanks', 'farewell', 'greeting') if kind in social_kinds)
        return Decision("preset", category, "S-" + category)
    # Protect quoted/code spans while splitting; mask examples only in their own technical clause.
    quotes = []
    def protect(match):
        quotes.append(match.group())
        return "\x00q" + str(len(quotes)-1) + "\x00"
    intent = QUOTED.sub(protect, text)
    daily, technical = [], False
    for part in SPLIT.split(intent):
        naked = re.sub(r"\x00q\d+\x00", "", part)
        clause = naked if TECH.search(naked) else re.sub(r"\x00q(\d+)\x00", lambda m: quotes[int(m[1])], part)
        match = next((rule for rule in RULES if rule.pattern.search(clause)), None)
        if TECH.search(clause):
            technical = True
            if match is None or TECH_APPLICATION.search(clause): continue
        if match: daily.append(match)
        elif re.search(r"教我.{0,8}(?:做饭|做菜|炒菜|煮饭)", clause): daily.append(RULES[0])
    if daily: return Decision("block", "mixed" if technical else daily[0].category, daily[0].id)
    return Decision(reason="technical" if technical else "uncertain")

def classify(question, previous=()):
    decision = current_decision(question)
    if decision.action != "allow" or not FOLLOW.fullmatch(normalize(question)): return decision
    for turn in reversed(previous):
        if turn.get("kind") == "verification": continue
        meta = turn.get("response", {}).get("boundary", {})
        old = Decision(meta["action"], meta["reason"], meta.get("rule_id", "")) if meta else current_decision(turn.get("question", ""))
        if old.action == "preset": continue
        if old.action == "block": return Decision("block", "daily_followup", old.rule_id)
        if FOLLOW.fullmatch(normalize(turn.get("question", ""))): continue
        break
    return decision

def model_history(previous, enabled=True):
    if not enabled: return previous
    kept, topics = [], []
    for turn in previous:
        if turn.get("kind") == "verification":
            kept.append(turn)
            continue
        decision = classify(turn.get("question", ""), topics)
        topics.append(turn)
        if not is_program_reply(turn.get("response", {})) and decision.action == "allow": kept.append(turn)
    return kept

def is_program_reply(response):
    return response.get("response_mode") in ("preset", "boundary")
