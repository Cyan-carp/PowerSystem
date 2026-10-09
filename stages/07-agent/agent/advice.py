"""Program-owned presentation metadata; never grants execution permissions."""
import re
from .schemas import Suggestion

OPERATION_NOTICE = "涉及实际设备操作：须由专业人员核对实际机型、厂家手册和现场条件后决定并执行。AI 不执行操作。"
DEVELOPMENT_NOTICE = "开发阶段尚未接入真实设备；三台仿真设备仅有参考型号手册，来源专业审核暂缓，公开资料及操作建议须由专业工程师复核。"
PHYSICAL_ACTION = re.compile(r"断电|停机|启机|启动|重启|复位|拆卸|拆开|更换|短接|拔插|改线|接线|调参|修改参数|远控|送电|合闸|拉闸|验电|测量|测试绝缘|清洁|清理|除冰|润滑|紧固|校准|登高|检修|维修|接地线")
INFORMATION_ONLY = re.compile(r"^(?:建议)?(?:人工)?(?:先|优先|请)?(?:(?:查阅|阅读|查看|核对|比对|收集|整理|记录|咨询|联系|提交|打开|展开|检索|搜索)(?:厂家|供应商|专业|引用|本条|相关|现有|受控|原始|平台|本轮|本次|完整|具体|公开|知识库)?(?:手册|资料|文档|来源|记录|日志|曲线|告警|页面|证据|工程师|专业人员|参数说明|现有数据|预测)(?:内容|章节|版本|原文|来源|记录|曲线|页面|日志|证据)?|确认告警(?:并标记已读)?|标记已读)[。；;.!！]?$" )
EQUIPMENT_CONTEXT = re.compile(r"检查|检测|测试|调整|开启|关闭|设备|风机|逆变器|光伏|机型|机舱|机柜|配电|电缆|冷却|风扇|部件|现场")


def equipment_operation(text):
    # Unknown wording is labelled conservatively, rather than rejected.
    if PHYSICAL_ACTION.search(text) or EQUIPMENT_CONTEXT.search(text):
        return True
    return not bool(INFORMATION_ONLY.fullmatch(text.strip()))


def suggestion(claim):
    return Suggestion(text=claim.text + "（需人工确认执行）", evidence_ids=claim.evidence_ids,
                      equipment_operation=equipment_operation(claim.text))
