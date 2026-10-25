"""Pure historical snapshot replay; threshold samples are proxies, not actual benefit."""
import hashlib,json,math,operator
from datetime import datetime

def evaluate(args,snapshot):
    points=snapshot.get('points')
    if snapshot.get('metric')!=args['metric'] or snapshot.get('device_id')!=args['device_id'] or not isinstance(points,list) or len(points)>5000:raise ValueError('invalid_history_snapshot')
    start,end=(datetime.fromisoformat(args[k]).timestamp()*1000 for k in ('start','end'))
    good=[];invalid=0;seen=set()
    for p in points:
        if isinstance(p,list) and len(p)==2:p=dict(ts=p[0],value=p[1])
        if not isinstance(p,dict):invalid+=1;continue
        ts=p.get('ts_ms',p.get('ts'));value=p.get('value')
        if isinstance(ts,str):
            try:
                parsed=datetime.fromisoformat(ts.replace('Z','+00:00'))
                ts=parsed.timestamp()*1000 if parsed.tzinfo is not None else None
            except ValueError:ts=None
        if type(ts) not in (int,float) or not math.isfinite(ts) or not start<=ts<end or ts in seen or type(value) not in (int,float) or not math.isfinite(value):invalid+=1;continue
        seen.add(ts);good.append((ts,value))
    good.sort();compare={'gt':operator.gt,'gte':operator.ge,'lt':operator.lt,'lte':operator.le}[args['operator']]
    baseline=[compare(v,args['baseline_threshold']) for _,v in good];candidate=[compare(v,args['threshold']) for _,v in good]
    expected=math.ceil((end-start)/1000/args['expected_interval_seconds']);missing=max(0,expected-len(good))
    gaps=sum(max(0,math.ceil((b[0]-a[0])/1000/args['expected_interval_seconds'])-1) for a,b in zip(good,good[1:]))
    raw=json.dumps(snapshot,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
    return dict(mode='simulation',parameters=args,snapshot_sha256=hashlib.sha256(raw).hexdigest(),snapshot=snapshot,coverage=dict(start_inclusive=args['start'],end_exclusive=args['end'],valid_samples=len(good),expected_samples=expected,missing_samples=missing,invalid_or_duplicate_or_outside=invalid,interior_gap_samples=gaps,complete=bool(good) and missing==0 and invalid==0),metrics=dict(baseline_hits=sum(baseline),candidate_hits=sum(candidate),new_hits=sum(b and not a for a,b in zip(baseline,candidate)),baseline_only_hits=sum(a and not b for a,b in zip(baseline,candidate))),assumptions=['独立持久历史快照；结束不含，单设备最多24小时5000点。','只比较采样阈值命中；无持续时长、滞回、现场真值、因果或收益结论。','expected_interval_seconds由本人明确设定；无样本或缺测不视为正常。','基准阈值为本人提供的候选比较值，不冒称线上规则历史版本。'],summary=f'有效样本{len(good)}，预计缺测{missing}；基准命中{sum(baseline)}，候选命中{sum(candidate)}。')
