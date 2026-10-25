"""S02 bounded energy objective, validated independently of model prose."""
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from .business_quality import number, positive_id, stamp, text, tool_name

BJT = timezone(timedelta(hours=8))


def requested_window(question, now=None):
    dates = re.findall(r'\d{4}-\d{2}-\d{2}', question)
    if len(dates) == 2:
        start, end = map(date.fromisoformat, dates)
        return start, end
    end = (now or datetime.now(timezone.utc)).astimezone(BJT).date()
    return end-timedelta(days=7), end


def finish_energy(question, result):
    if not re.search(r'发电量', question) or not re.search(r'7天|七天', question):
        return False
    calls = list(result.get('tool_calls', []))
    for card in result.get('task_cards', []):
        # Historical/sensitive reports cannot become a new current objective.
        if card.get('historical') or any(card.get(k) for k in ('security_reviews','inspection_report','infrastructure_evidence','operation_evidence','approvals')):
            continue
        calls.extend(card.get('tool_calls', []))
    reason = 'energy_evidence_missing'
    answer = '七天发电量目标尚未完成：未取得对应设备与日期窗口的 energy_daily 聚合。瞬时功率或24小时遥测不能替代。'
    quality = {'kind':'energy_daily','complete':False,'reason':reason}
    try:
        start, end = requested_window(question, datetime.fromisoformat(result['_energy_now']) if result.get('_energy_now') else None)
        energy = next(c for c in reversed(calls) if tool_name(c)=='get_energy_report' and c.get('status')=='ok')
        args, data = energy['arguments'], energy['result']
        did = positive_id(args.get('device_id'))
        if not did or data.get('device_id') != did: raise ValueError('device_mismatch')
        explicit=re.search(r'设备\s*(?:ID|编号)\s*[:：]?\s*(\d+)',question,re.I)
        if explicit and int(explicit[1])!=did: raise ValueError('explicit_device_mismatch')
        if data.get('scope','all')!=args.get('scope','all') or data.get('station_code','')!=args.get('station_code',''):
            raise ValueError('response_filter_mismatch')
        ordinal = re.search(r'第(一|二|[12])台', question)
        if ordinal:
            n = 1 if ordinal[1] in ('一','1') else 2
            ledger = next(c for c in calls if tool_name(c)=='list_devices' and c.get('status')=='ok'
                          and c.get('arguments',{}).get('page',1)==1)
            rows = ledger['result']['list']
            ids = [positive_id(r.get('id')) for r in rows]
            if len(ids)<n or None in ids or ids!=sorted(set(ids)) or ids[n-1]!=did:
                raise ValueError('ordinal_mismatch')
            for key in ('scope','station_code'):
                if ledger.get('arguments',{}).get(key, 'all' if key=='scope' else '') != args.get(key,'all' if key=='scope' else ''):
                    raise ValueError('filter_mismatch')
        for key, value in [('start_date',start.isoformat()),('end_date',end.isoformat())]:
            if args.get(key)!=value or data.get(key)!=value: raise ValueError('window_mismatch')
        if (end-start).days!=7 or data.get('days')!=7 or data.get('rule_version')!='energy-daily-v1':
            raise ValueError('window_or_version_invalid')
        if [r.get('device_id') for r in data['devices']] != [did]: raise ValueError('selected_devices_mismatch')
        daily = data['daily']
        expected = [(start+timedelta(days=i)).isoformat() for i in range(7)]
        if [r.get('date') for r in daily] != expected: raise ValueError('daily_window_invalid')
        if data.get('complete') is not True or data.get('missing') != [] or any(
            r.get('complete') is not True or r.get('device_count')!=1 or r.get('valid_device_count')!=1 for r in daily):
            raise ValueError('aggregate_incomplete')
        values = [number(r.get('kwh')) for r in daily]
        total = number(data.get('total_kwh'))
        if total is None or any(v is None or v<0 for v in values) or abs(sum(values,Decimal(0))-total)>Decimal('0.000001'):
            raise ValueError('aggregate_total_mismatch')
        if not stamp(data.get('aggregated_at')) or not stamp(energy.get('collected_at')): raise ValueError('timestamp_missing')
        answer = ('设备 ID '+str(did)+' 的近7天汇总发电量：**'+str(total)+' kWh**。\n\n'
                  '统计窗口：北京时间 '+start.isoformat()+' 00:00（含）至 '+end.isoformat()+' 00:00（不含），最近7个已结束自然日。\n\n'
                  '来源：energy_daily，口径 '+data['rule_version']+'；聚合更新时间 '+text(data['aggregated_at'])+'。7天均有有效日聚合记录；这不表示原始遥测无缺口。')
        quality.update(complete=True,reason=None,device_id=did,start_date=start.isoformat(),end_date=end.isoformat(),total_kwh=float(total))
    except (StopIteration, KeyError, TypeError, ValueError, AttributeError) as exc:
        quality['reason'] = str(exc) if isinstance(exc, ValueError) else reason
        answer += '\n\n核验结果：'+text(quality['reason'])+'。未将部分总量或缺测当作完整七天结果。'
    result.update(answer_markdown=answer,status='answered',objective_quality=quality)
    result.pop('_energy_now',None)
    return True
