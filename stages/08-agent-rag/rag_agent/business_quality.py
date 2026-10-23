"""BND-C01/C02/C03: facts come from actual calls; critical prose comes from code.

Shared with Operation via a packaged copy of this exact module. Standard library only.
Never execute or interpret document content as instructions.
"""
import hashlib
import json
import math
import re
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation

VERSION = 'bnd-business-v1'
BUSINESS = frozenset({'list_devices', 'get_device', 'get_telemetry', 'get_prediction', 'list_alarms', 'get_alarm_detail', 'get_alarm_statistics', 'get_dashboard_summary', 'list_stations', 'get_dashboard_overview', 'get_alarm_rules'})
UNITS = {'voltage': 'V', 'current': 'A', 'temperature': '°C', 'power': 'kW', 'ai_failure_risk': '1'}
EXPLANATIONS = {
    'facts_only': '',
    'threshold_scope': '单条规则的命中或未命中不能证明设备整体健康，额定参数也不等于现场告警阈值。',
    'alarm_lifecycle': '告警确认与恢复是不同事件，管理关闭不表示故障已消除。',
    'manual_scope': '资料参数仅在登记的型号、区域、版本和适用条件匹配时可作对应参考；仿真资料不能证明真实硬件能力。',
}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def text(value):
    return re.sub(r'[\x00-\x1f\x7f`|<>]', ' ', str(value))[:160]


def stamp(value):
    try:
        d = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return d.astimezone(timezone.utc) if d.tzinfo else None
    except (ValueError, TypeError, AttributeError):
        return None


def number(value):
    if type(value) not in (int, float, str) or isinstance(value, str) and len(value) > 60:
        return None
    try:
        v = Decimal(str(value))
        return v if v.is_finite() and math.isfinite(float(v)) else None
    except (InvalidOperation, OverflowError, ValueError):
        return None


def positive_id(value):
    return value if type(value) is int and value > 0 else None


def tool_name(call):
    return str(call.get('name', '')).removeprefix('platform.').removeprefix('platform__')


def business_request(question, calls=(), previous=()):
    if any(tool_name(c) in BUSINESS for c in calls):
        return True
    target = re.search(r'\b[A-Z]{1,8}-\d[\w-]*|SUN\d[\w-]+|第[一二三四五六七八九十\d]+台|这台|该设备|这台设备', question, re.I)
    read = re.search(r'查|现在|当前|最新|状态|健康|告警|现场|适用|手册|阈值', question)
    technical = re.search(r'原理|是什么|如何实现|怎么计算|如何比较|含义|区别', question)
    if target or read and not technical and re.search(r'设备|场站|电站|逆变器|遥测|告警|台账', question):
        return True
    if re.search(r'现在|刚刚|它|确定|真的吗|再查|还有|继续|状态', question):
        return any(t.get('response', {}).get('business_quality') or business_request(t.get('question','')) or any(tool_name(c) in BUSINESS for c in t.get('response', {}).get('tool_calls', [])) for t in previous)
    return False


def applicability(device, meta):
    if not isinstance(meta, dict):
        return 'insufficient', '资料缺少登记的适用性元数据'
    kind = meta.get('source_type')
    if kind == 'simulation_reference' or device.get('is_simulated') is True:
        return 'simulation_reference', '仅为仿真参考，不能证明真实硬件能力'
    if kind == 'project_summary':
        return 'project_summary', '项目摘要，不是厂家手册，不能提供现场参数依据'
    if kind != 'manufacturer_manual':
        return 'insufficient', '资料类型未知，不能冒称厂家手册'
    identity = {'manufacturer': device.get('manufacturer') or device.get('vendor'), 'full_model': device.get('model'), 'region_variant': device.get('region_variant')}
    for key, actual in identity.items():
        if actual and meta.get(key) and actual != meta[key]:
            return 'mismatch', '厂家、完整型号或区域变体不匹配'
    expected = device.get('manual_version')
    if expected and meta.get('document_version') and expected != meta['document_version']:
        return 'mismatch', '登记的手册版本不匹配'
    required = ('manufacturer', 'full_model', 'region_variant', 'document_number', 'document_version')
    if not all(meta.get(k) for k in required) or not all(identity.values()) or meta.get('registered') is not True or not isinstance(meta.get('conditions'), dict):
        return 'insufficient', '型号、区域、编号、版本或登记适用条件不足'
    for key, expected in meta['conditions'].items():
        if key not in device:
            return 'insufficient', '设备缺少适用条件字段'
        if device[key] != expected:
            return 'mismatch', '设备不满足登记的适用条件'
    return 'match', '厂家、完整型号、区域、资料版本及登记条件匹配；额定值仍不能代替现场阈值'


def build_quality(calls, historical=False):
    facts, missing, devices, latest, rules, documents = [], [], {}, {}, [], []
    device_refs = {}
    evidence = []
    success = failed = 0

    def add(kind, data, line, refs, device_id=None):
        fact = {'kind': kind, 'device_id': device_id, 'call_refs': refs, 'data': data, 'text': line}
        fact['id'] = 'F' + digest(fact)[:20]
        facts.append(fact)

    def compare(metric, value, threshold, operator, unit):
        a, b = number(value), number(threshold)
        if a is None or b is None or operator not in ('>', '>=', '<', '<=') or not unit or unit != UNITS.get(metric):
            return None
        relation = '>' if a > b else '<' if a < b else '='
        hit = {'>': a > b, '>=': a >= b, '<': a < b, '<=': a <= b}[operator]
        return {'metric': metric, 'value': float(a), 'threshold': float(b), 'unit': unit, 'operator': operator, 'relation': relation, 'triggered': hit}

    for index, call in enumerate(calls):
        name = tool_name(call)
        if name not in BUSINESS and name not in ('search_knowledge', 'knowledge.search'):
            continue
        ref = 'call:' + str(call.get('call_id') or digest({'index': index, 'call': call})[:24])
        if name in BUSINESS:
            if call.get('status') != 'ok':
                failed += 1
                missing.append(name + ' 调用未成功')
                continue
            success += 1
        data, args = call.get('result'), call.get('arguments', {})
        if call.get('status') != 'ok' or not isinstance(data, dict) or data.get('truncated'):
            missing.append(name + ' 没有完整结构化结果')
            continue
        collected = call.get('collected_at')
        if not stamp(collected):
            missing.append(name + ' 缺少有效查询时间')
            continue
        evidence.append({'ref': ref, 'tool': name, 'queried_at': collected, 'arguments': args})
        if name in ('get_device', 'list_devices'):
            for device in data.get('list', []) if name == 'list_devices' else [data]:
                if not isinstance(device, dict) or not positive_id(device.get('id')):
                    continue
                if args.get('device_id') and args['device_id'] != device['id']:
                    missing.append('设备返回与请求目标不一致'); continue
                devices[device['id']] = device
                device_refs[device['id']] = ref
                add('device', {'device_id': device['id'], 'device_code': device.get('device_code'), 'queried_at': collected}, '台账设备 ' + text(device.get('device_code') or device['id']) + '（ID ' + str(device['id']) + '）；查询时间 ' + text(collected) + '。', [ref], device['id'])
        elif name == 'list_stations':
            rows = data.get('list', [])
            values = [{'station_code': row.get('station_code'), 'scope': row.get('scope'),
                'expected_device_count': row.get('expected_device_count'),
                'registered_device_count': row.get('registered_device_count')}
                for row in rows if isinstance(row, dict)]
            add('station_scope', {'scope': args.get('scope', 'all'), 'total': data.get('total'), 'stations': values},
                '场站清单查询返回 ' + str(len(values)) + ' 个场站；范围 ' + text(args.get('scope', 'all')) + '；查询时间 ' + text(collected) + '。', [ref])
        elif name == 'get_dashboard_overview':
            summary = data.get('summary') if isinstance(data.get('summary'), dict) else {}
            values = {k:data.get(k) for k in ('scope', 'station_code', 'partial', 'expected_device_count', 'registered_device_count', 'observed_device_count', 'computed_at')}
            values['summary'] = {k:summary.get(k) for k in ('device_total', 'online', 'offline', 'fault', 'active_alarms', 'current_power_kw', 'today_energy_kwh', 'health_score_percent')}
            add('aggregate', {'values':values, 'filters':args},
                '当前总览聚合（仅限本次筛选范围）：' + text(json.dumps(values, ensure_ascii=False)) + '。', [ref])
        elif name == 'get_telemetry':
            device_id = positive_id(args.get('device_id'))
            if not device_id or data.get('device_id') != device_id or args.get('mode', 'latest') != 'latest':
                missing.append('遥测不是已绑定设备的最新样本'); continue
            ts = data.get('ts_ms')
            if type(ts) not in (int, float) or not math.isfinite(ts) or ts <= 0:
                missing.append('遥测采样时间缺失或异常'); continue
            try:
                sampled = datetime.fromtimestamp(ts/1000, timezone.utc)
            except (ValueError, OverflowError, OSError):
                missing.append('遥测采样时间异常'); continue
            received, queried = stamp(data.get('received_at')), stamp(collected)
            fresh = bool(received and -1 <= (queried-received).total_seconds() <= 15 and -60 <= (queried-sampled).total_seconds() <= 15)
            latest[device_id] = (data, ref, fresh)
            values = {k: data[k] for k in UNITS if k in data and number(data[k]) is not None}
            body = '；'.join(text(k) + ' ' + text(v) + ' ' + UNITS[k] for k, v in values.items())
            status = {0:'停机（非发电声明）', 1:'运行', 2:'遥测故障状态'}.get(data.get('status'), '状态未知')
            add('telemetry', {'values': values, 'status': data.get('status'), 'ts_ms': ts, 'received_at': data.get('received_at'), 'queried_at': collected, 'fresh': fresh},
                '设备 ID ' + str(device_id) + ' 的查询记录：' + body + '；' + status + '；采样时间 ' + sampled.isoformat() + '（ts_ms=' + str(ts) + '）；接收时间 ' + text(data.get('received_at')) + '；查询时间 ' + text(collected) + ('；样本新鲜。' if fresh else '；样本过期或时间不完整，不能代表当前。'), [ref], device_id)
        elif name == 'get_alarm_rules':
            for rule in data.get('list', []):
                if isinstance(rule, dict) and positive_id(rule.get('device_id')) and (not args.get('device_id') or rule['device_id'] == args['device_id']):
                    rules.append((rule, ref))
            if data.get('page', 1) != 1 or type(data.get('total')) is not int or len(data.get('list', [])) < data['total']:
                missing.append('告警规则仅部分分页，不代表全量')
        elif name in ('list_alarms', 'get_alarm_detail'):
            rows = data.get('list', []) if name == 'list_alarms' else [data]
            if name == 'list_alarms':
                total = data.get('total')
                complete = type(total) is int and data.get('page', 1) == 1 and len(rows) == total
                add('alarm_scope', {'filters': args, 'returned': len(rows), 'reported_total': total, 'complete': complete}, '告警查询返回 ' + str(len(rows)) + ' 条；' + ('本次筛选范围已完整返回。' if complete else '仅为部分分页，不能代表全部告警。'), [ref], positive_id(args.get('device_id')))
                if not complete: missing.append('告警分页不完整')
            for alarm in rows:
                if not isinstance(alarm, dict) or not positive_id(alarm.get('device_id')): continue
                device_id = alarm['device_id']
                if args.get('device_id') and args['device_id'] != device_id: missing.append('告警目标不符'); continue
                state, reason = alarm.get('status'), alarm.get('close_reason')
                ended = stamp(alarm.get('recovered_at'))
                if state == 'unhandled' and not alarm.get('recovered_at') and not alarm.get('acked_at'): label = '未确认且未恢复（活动告警）'
                elif state == 'acked' and not alarm.get('recovered_at'): label = '已确认但未恢复（活动告警）'
                elif state == 'recovered' and ended:
                    label = {'telemetry_recovered':'平台记录已结束：遥测恢复', 'prediction_risk_resolved':'平台记录已结束：预测风险下降', 'rule_disabled':'平台记录已结束：规则禁用，属于管理关闭', 'rule_deleted':'平台记录已结束：规则删除，属于管理关闭', 'device_deleted':'平台记录已结束：设备删除，属于管理关闭'}.get(reason, '平台记录已结束：原因未知，不能认定故障消除')
                    label += '；历史结束事件不计入当前活动告警'
                else: label = '状态与生命周期不完整或冲突，不能判定确认/恢复'
                payload = {k: alarm.get(k) for k in ('id', 'status', 'close_reason', 'triggered_at', 'acked_at', 'recovered_at')}
                add('alarm', payload, '设备 ID ' + str(device_id) + ' 告警 ' + text(alarm.get('id', '?')) + '：' + label + '；触发 ' + text(alarm.get('triggered_at')) + '，确认 ' + text(alarm.get('acked_at')) + '，平台结束 ' + text(alarm.get('recovered_at')) + '。', [ref], device_id)
                compared = compare(alarm.get('metric'), alarm.get('value'), alarm.get('threshold'), alarm.get('rule_operator'), UNITS.get(alarm.get('metric')))
                if compared and alarm.get('rule_version'):
                    compared['rule_version'] = alarm['rule_version']
                    add('historical_threshold', compared, comparison_text(compared, '告警触发时快照'), [ref], device_id)
                else: missing.append('历史告警缺少比较符/规则版本等快照，不使用当前规则反推')
        elif name in ('search_knowledge', 'knowledge.search'):
            documents.extend((row, ref) for row in data.get('results', []) if isinstance(row, dict))
        elif name == 'get_prediction':
            device_id = positive_id(args.get('device_id'))
            if device_id and data.get('device_id') == device_id:
                add('prediction', {k:data.get(k) for k in ('probability', 'threshold', 'risk_level', 'window_end_ms', 'stale', 'model_version')}, '设备 ID ' + str(device_id) + ' 预测记录：概率 ' + text(data.get('probability')) + '，模型阈值 ' + text(data.get('threshold')) + '，风险标签 ' + text(data.get('risk_level')) + '，窗口时间戳 ' + text(data.get('window_end_ms')) + '，stale=' + text(data.get('stale')) + '；预测不等于真实故障。', [ref], device_id)
        elif name in ('get_dashboard_summary', 'get_alarm_statistics'):
            fields = ('device_total', 'online', 'offline', 'fault', 'active_alarms') if name == 'get_dashboard_summary' else ('total_events', 'complete', 'start', 'end')
            values = {k:data[k] for k in fields if k in data and type(data[k]) in (int, float, bool, str, type(None))}
            add('aggregate', {'values':values, 'filters':args}, '聚合查询记录（仅限本次筛选范围）：' + text(json.dumps(values, ensure_ascii=False)) + '。', [ref])

    for rule, ref in rules:
        device_id = rule['device_id']
        if rule.get('enabled') is not True:
            missing.append('规则 ' + text(rule.get('id')) + ' 未启用，不用于当前比较'); continue
        if device_id not in latest or not latest[device_id][2]:
            missing.append('规则比较缺少同设备的新鲜遥测'); continue
        sample, sample_ref, _ = latest[device_id]
        compared = compare(rule.get('metric'), sample.get(rule.get('metric')), rule.get('threshold'), rule.get('operator'), rule.get('unit'))
        if not compared or not rule.get('version'):
            missing.append('规则缺少合法单位、比较符、数值或版本，无法比较'); continue
        compared.update(rule_id=rule.get('id'), rule_version=rule['version'])
        add('threshold', compared, comparison_text(compared, '取证时的登记规则'), [ref, sample_ref], device_id)
    if latest and not rules:
        missing.append('未取得当前告警规则，不能据遥测自行设定现场阈值或断言整体健康')
    for row, ref in documents:
        targets = devices.items() if devices else [(None, {})]
        for device_id, device in targets:
            state, reason = applicability(device, row.get('applicability'))
            data = {'source_id':row.get('id'), 'source_version':row.get('version'), 'applicability':row.get('applicability'), 'state':state}
            refs = [ref] + ([device_refs[device_id]] if device_id in device_refs else [])
            add('applicability', data, '资料 ' + text(row.get('id', '?')) + '（来源版本 ' + text(row.get('version')) + '）：' + reason + '。', refs, device_id)
    if len(facts) > 160:
        facts = facts[:160]; missing.append('业务事实超过输出预算，结果不完整')
    state = 'not_queried' if success+failed == 0 else 'failed' if success == 0 else 'partial_failed' if failed else 'queried'
    payload = {'version': VERSION, 'state': state, 'scope': 'historical' if historical else 'current_round', 'facts': facts, 'missing': list(dict.fromkeys(missing)), 'evidence': evidence}
    payload['digest'] = digest(payload)
    return payload


def comparison_text(data, prefix):
    return prefix + '：' + text(data['metric']) + ' ' + str(data['value']) + ' ' + data['unit'] + ' ' + data['relation'] + ' ' + str(data['threshold']) + ' ' + data['unit'] + '；规则 ' + text(data['rule_version']) + ' 指定 ' + data['operator'] + '，本条' + ('命中' if data['triggered'] else '未命中') + '。不能据单条规则推断整体健康。'


def valid_quality(quality):
    try:
        return isinstance(quality, dict) and quality.get('version') == VERSION and quality.get('digest') == digest({k:v for k,v in quality.items() if k != 'digest'})
    except (TypeError, ValueError):
        return False


def alarm_category(fact):
    data = fact['data']
    if data.get('status') == 'unhandled' and not data.get('acked_at') and not data.get('recovered_at'):
        return '未确认且未恢复'
    if data.get('status') == 'acked' and not data.get('recovered_at'):
        return '已确认但未恢复'
    if data.get('status') == 'recovered' and stamp(data.get('recovered_at')):
        return {'telemetry_recovered': '遥测恢复', 'prediction_risk_resolved': '预测风险下降',
                'rule_disabled': '管理关闭', 'rule_deleted': '管理关闭', 'device_deleted': '管理关闭'}.get(data.get('close_reason'), '原因未知')
    return '状态与生命周期不完整或冲突'


def alarm_line(fact):
    def display_time(value):
        parsed = stamp(value)
        return parsed.astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S +08:00') if parsed else '未记录或时间无效'
    # Keep the validated lifecycle explanation; format timestamps for the answer
    # without changing the raw fact or its signed digest.
    label = fact['text'].split('；触发 ', 1)[0]
    data = fact['data']
    return '- ' + label + '；触发 ' + display_time(data.get('triggered_at')) + '，确认 ' + display_time(data.get('acked_at')) + '，平台结束 ' + display_time(data.get('recovered_at')) + '。'


def answer_time(value, milliseconds=False):
    try:
        parsed = datetime.fromtimestamp(value / 1000, timezone.utc) if milliseconds and number(value) is not None else stamp(value)
        return parsed.astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S（北京时间）') if parsed else '时间未记录'
    except (ValueError, OverflowError, OSError, TypeError):
        return '时间无效'


def brief_fact_text(fact):
    """Business prose only; raw values, timestamps and IDs remain in facts."""
    data, kind = fact['data'], fact['kind']
    metrics = {'voltage':'电压', 'current':'电流', 'temperature':'温度', 'power':'功率', 'ai_failure_risk':'预测风险'}
    if kind == 'telemetry':
        status = {0:'停机（不表示正在发电）', 1:'运行', 2:'遥测故障状态'}.get(data.get('status'), '状态未知')
        values = '，'.join(metrics.get(k,k) + ' ' + str(v) + ' ' + UNITS.get(k,'') for k,v in data.get('values',{}).items())
        return '采样记录为' + status + ('；' + values if values else '') + '。采样于 ' + answer_time(data.get('ts_ms'), True) + ('，样本新鲜。' if data.get('fresh') is True else '；样本过期或时间不完整，不能代表当前。')
    if kind == 'prediction':
        def percent(value):
            n = number(value)
            if n is None or n < 0 or n > 1:return '未知'
            return ('小于 0.01%' if 0 < n < Decimal('0.0001') else format(n * 100, '.2f') + '%')
        risk = {'low':'低', 'medium':'中', 'high':'高'}.get(data.get('risk_level'), '未知')
        return '预测风险标签为' + risk + '（概率约 ' + percent(data.get('probability')) + '，模型阈值 ' + percent(data.get('threshold')) + '）；' + ('' if data.get('stale') is False else '预测已过期或新鲜度未知，不能代表当前；') + '预测不等于真实故障。'
    if kind == 'alarm_scope':
        returned, total = data.get('returned'), data.get('reported_total')
        body = '告警查询返回 ' + str(returned) + ' 条'
        if type(total) is int:body += '（本次筛选共 ' + str(total) + ' 条）'
        return body + ('；已完整返回本次筛选范围。' if data.get('complete') is True else '；仅为部分分页，不能代表全部告警。')
    if kind in ('threshold','historical_threshold'):
        line = fact['text']
        for metric,label in metrics.items():line = line.replace(metric, label)
        return line
    if kind == 'applicability':
        return '资料适用性：' + fact['text'].split('：',1)[-1]
    if kind == 'aggregate':
        labels={'device_total':'设备总数','online':'在线','offline':'离线','fault':'故障','active_alarms':'活动告警','total_events':'告警事件数','complete':'完整性','start':'统计起点','end':'统计终点'}
        def value(k,v):
            if k in ('start','end'):return answer_time(v)
            if k == 'complete':return '已完整返回' if v is True else '未确认完整'
            return '未知' if v is None else text(v)
        return '本次筛选范围的统计：' + '，'.join(labels.get(k,k) + ' ' + value(k,v) for k,v in data.get('values',{}).items()) + '。'
    return fact['text']


def answer_fact_lines(quality, facts, question):
    """Render business summaries; retain full facts and bindings outside prose."""
    device_order = list(dict.fromkeys(f.get('device_id') for f in facts if f['kind'] != 'device' and f.get('device_id') is not None))
    facts = sorted(facts, key=lambda f: device_order.index(f.get('device_id')) if f.get('device_id') in device_order else len(device_order))
    list_refs = {e['ref'] for e in quality.get('evidence', []) if e['tool'] == 'list_alarms'}
    # Delegated/historical task facts retain call_refs even when their evidence
    # array is not copied into the Supervisor quality object.
    list_refs.update(ref for f in facts if f['kind'] == 'alarm_scope' for ref in f['call_refs'])
    detail_refs = {e['ref'] for e in quality.get('evidence', []) if e['tool'] == 'get_alarm_detail'}
    list_alarms = [f for f in facts if f['kind'] == 'alarm' and set(f['call_refs']) & list_refs and not set(f['call_refs']) & detail_refs]
    history_requested = bool(re.search(r'历史|已结束|已恢复|恢复记录|结束记录|关闭记录|过去|昨天|昨日|前天|上周|上月|近\s*[一二三四五六七八九十\d]+\s*[天日周月]|最近\s*[一二三四五六七八九十\d]+\s*[天日周月]|\d{4}-\d{2}-\d{2}', question))
    candidates = list_alarms if history_requested else [f for f in list_alarms if alarm_category(f) in ('未确认且未恢复', '已确认但未恢复', '状态与生命周期不完整或冲突')]
    # Model ordering cannot bury an unresolved alarm behind historical rows.
    candidates.sort(key=lambda f: alarm_category(f) not in ('未确认且未恢复', '已确认但未恢复', '状态与生命周期不完整或冲突'))
    shown = {f['id'] for f in candidates[:5]}
    grouped = {}
    for fact in list_alarms:
        key = (fact.get('device_id'), tuple(fact['call_refs']), bool(fact.get('historical')))
        grouped.setdefault(key, []).append(fact)
    summaries = {}
    for group in grouped.values():
        counts = {}
        for fact in group:
            category = alarm_category(fact)
            counts[category] = counts.get(category, 0) + 1
        body = '、'.join(k + ' ' + str(v) + ' 条' for k, v in counts.items())
        prefix = '历史任务当时返回的记录' if group[0].get('historical') or quality['scope'] == 'historical' else '本次返回的记录'
        line = '- 设备 ID ' + str(group[0].get('device_id')) + '，' + prefix + '中：' + body + '。仅统计已返回记录，不代表当前活动告警全量。'
        if any(k in counts for k in ('遥测恢复', '预测风险下降', '管理关闭', '原因未知')):
            line += '历史结束事件不计入当前活动告警；管理关闭或原因未知不能证明故障消除。'
        summaries[group[0]['id']] = line
    list_ids = {f['id'] for f in list_alarms}
    names = {}
    for fact in facts:
        if fact['kind'] == 'device':
            names.setdefault(fact.get('device_id'), set()).add(text(fact['data'].get('device_code') or '设备 ID '+str(fact.get('device_id'))))
    substantive = {f.get('device_id') for f in facts if f['kind'] not in ('device','applicability') and f.get('device_id') is not None}
    lines, seen_text, seen_devices = [], set(), set()
    def append_line(line):
        if line not in seen_text:
            lines.append(line);seen_text.add(line)
    if not substantive and names:
        labels = [' / '.join(sorted(v)) for v in names.values()]
        append_line('已取得台账记录：' + '、'.join(labels[:5]) + ('等 ' + str(len(labels)) + ' 台设备' if len(labels)>5 else '') + '；台账记录本身不能证明设备当前运行状态。')
    snapshots = 0
    for fact in facts:
        device_id = fact.get('device_id')
        if fact['kind'] == 'device':
            continue
        if device_id is not None and device_id not in seen_devices:
            label = ' / '.join(sorted(names.get(device_id, {'设备 ID '+str(device_id)})))
            append_line(label + ('（历史任务当时记录）：' if fact.get('historical') or quality['scope']=='historical' else '：'))
            seen_devices.add(device_id)
        if fact['id'] in summaries:
            append_line(summaries[fact['id']])
        if fact['id'] in list_ids:
            if fact['id'] in shown:
                append_line(alarm_line(fact))
        elif fact['kind'] == 'historical_threshold' and set(fact['call_refs']) & list_refs and not set(fact['call_refs']) & detail_refs:
            if history_requested and snapshots < 5:
                append_line('- ' + brief_fact_text(fact))
                snapshots += 1
        else:
            append_line(alarm_line(fact) if fact['kind'] == 'alarm' else '- ' + brief_fact_text(fact))
    if len(list_alarms) > len(shown):
        lines.append('告警列表正文仅展开' + ('最多 5 条明细' if history_requested else '最多 5 条活动或状态异常明细，历史结束记录默认不逐条展开') + '；完整返回记录与调用引用保留在结构化事实中，查询本身的分页范围不变。')
    return lines


def render_quality(quality, selection=None, historical=False, question=''):
    """Unrecognized model fields/prose never pass through to business conclusions."""
    allowed = {f['id'] for f in quality['facts']}
    valid = isinstance(selection, dict) and set(selection) == {'fact_refs', 'explanation_type'} and isinstance(selection.get('fact_refs'), list) and all(isinstance(r, str) and r in allowed for r in selection['fact_refs']) and selection.get('explanation_type') in EXPLANATIONS and (bool(selection['fact_refs']) or not allowed)
    # Model selection only controls ordering. The program summarizes alarm lists;
    # critical comparisons, applicability and completeness warnings remain mandatory.
    refs = selection['fact_refs'] if valid else []
    facts = sorted(quality['facts'], key=lambda f: (refs.index(f['id']) if f['id'] in refs else len(refs)))
    header = '历史任务事实（仅说明当时记录，不能代表当前）' if historical or quality['scope'] == 'historical' else {'not_queried':'本轮未取得平台业务证据，不能报告当前设备状态', 'failed':'本轮业务查询失败，不能声称已查到当前状态', 'partial_failed':'本轮业务查询部分失败，结论仅限已取得事实', 'queried':''}[quality['state']]
    lines = ([header + '。'] if header else []) + answer_fact_lines(quality, facts, question)
    lines += ['- 证据不足：' + m for m in quality['missing']]
    if valid and EXPLANATIONS[selection['explanation_type']]: lines.append(EXPLANATIONS[selection['explanation_type']])
    # Validation state is already exposed in business_quality. Keep technical
    # selection failures out of business prose without accepting model prose.
    return '\n\n'.join(lines), 'validated' if valid else 'degraded'


def quality_summary(quality):
    if not valid_quality(quality): raise ValueError('business_quality_binding_mismatch')
    return {'version': VERSION, 'digest': quality['digest'], 'state': quality['state'], 'fact_refs': [f['id'] for f in quality['facts']]}


def finalize_diagnosis(result, question):
    if not business_request(question, result.get('tool_calls', [])):
        return
    quality = build_quality(result.get('tool_calls', []))
    if result.get('error'): quality['state'] = 'partial_failed' if quality['facts'] else 'failed'
    answer, state = render_quality(quality, result.pop('_business_selection', None), question=question)
    quality['render_state'] = state
    quality['digest'] = digest({k:v for k,v in quality.items() if k != 'digest'})
    result['business_quality'] = quality
    result['diagnosis'] = {'summary': answer, 'hypotheses': [], 'recommendations': [], 'clarification': '请明确设备编码或先查询台账定位目标。' if not quality['facts'] and quality['state'] == 'not_queried' else None}


def guard_answer(question, result, previous=()):
    cards = result.get('task_cards', [])
    stored = [c for c in cards if valid_quality(c.get('business_quality'))]
    invalid = any(c.get('business_quality') and not valid_quality(c['business_quality']) for c in cards)
    for card in cards:
        if card.get('business_quality') is None and any(tool_name(c) in BUSINESS for c in card.get('tool_calls', [])):
            stored.append({**card, 'historical': True, 'business_quality': build_quality(card['tool_calls'], historical=True)})
    if not stored and not invalid and not business_request(question, result.get('tool_calls', []), previous) and not re.search(r'(?:INV|DEV)-[\w-]+|SUN\d[\w-]+', result.get('answer_markdown', ''), re.I):
        return False
    quality = build_quality(result.get('tool_calls', []))
    if any(not c.get('historical') and c.get('status') in ('failed','timeout','cancelled') for c in cards):
        quality['state'] = 'partial_failed' if quality['facts'] else 'failed'
    if invalid: quality['missing'].append('任务质量字段校验失败，不使用其结论')
    seen = {f['id'] for f in quality['facts']}
    for card in stored:
        source = card['business_quality']
        for fact in source['facts']:
            if fact['id'] in seen: continue
            fact = dict(fact)
            if card.get('historical'):
                fact['historical'] = True
                fact['text'] = '历史任务 ' + text(card.get('task_id')) + ' 的当时事实：' + fact['text']
            quality['facts'].append(fact); seen.add(fact['id'])
        quality['missing'].extend(source['missing'])
    quality['missing'] = list(dict.fromkeys(quality['missing']))
    # Delegated successful reads count as this round's evidence too. The main
    # agent's get-task call is not itself a platform business read.
    current_states = [c['business_quality']['state'] for c in stored if not c.get('historical')]
    if quality['facts'] and current_states:
        quality['state'] = 'partial_failed' if quality['state'] in ('failed', 'partial_failed') or any(s in ('failed', 'partial_failed') for s in current_states) else 'queried'
    if len(quality['facts']) > 160:
        quality['facts'] = quality['facts'][:160]; quality['missing'].append('业务事实超过输出预算，结果不完整')
    quality['source_digests'] = [c['business_quality']['digest'] for c in stored]
    historical = bool(stored) and all(c.get('historical') for c in stored) and not any(tool_name(c) in BUSINESS for c in result.get('tool_calls', []))
    if historical: quality['scope'] = 'historical'
    try: selection = json.loads(result.get('answer_markdown', ''))
    except (TypeError, ValueError): selection = None
    answer, state = render_quality(quality, selection, historical, question=question)
    # Queue state is a program fact, not an inferred successful query.
    active = [c for c in cards if not c.get('historical') and c.get('status') in ('queued', 'planning', 'executing', 'awaiting_approval')]
    if active and not quality['facts']:
        answer = '运维任务已受理，正在排队或执行。受理成功不代表查询完成；本轮尚无可用于当前结论的业务事实。'
    quality['render_state'] = state
    quality['digest'] = digest({k:v for k,v in quality.items() if k != 'digest'})
    result.update(answer_markdown=answer, business_quality=quality, status='answered')
    return True
