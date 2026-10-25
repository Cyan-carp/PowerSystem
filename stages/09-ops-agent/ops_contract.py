"""MA-M2 wire contract shared by model-free adapter and remote collector (Python 3.11)."""
import base64
import hashlib
import hmac
import json
import math
import re
import time
from datetime import datetime
from uuid import UUID

ASSET_PATTERN = r"[a-z][a-z0-9_-]{0,79}"
KINDS = ('host', 'service', 'container', 'database')
TOOLS = {
 'linux.get_cpu_usage': ('host', '读取主机CPU采样及负载'),
 'linux.get_memory_usage': ('host', '读取主机内存及交换空间'),
 'linux.get_disk_status': ('host', '读取登记文件系统容量'),
 'linux.list_services': ('host', '读取本项目登记的systemd服务状态'),
 'linux.get_system_logs': ('service', '读取登记项目服务的安全事件摘要，过滤不代表完整日志'),
 'docker.list_containers': ('host', '读取本项目已登记容器状态，不发现其他项目'),
 'docker.inspect_container': ('container', '读取容器安全状态、镜像和健康，排除环境变量和挂载'),
 'docker.get_container_stats': ('container', '读取容器CPU与内存，不能代表主机'),
 'docker.get_container_logs': ('container', '读取容器安全事件摘要，过滤不代表完整日志'),
 'postgresql.get_db_status': ('database', '固定数据库健康与聚合统计，不读取业务记录'),
 'postgresql.get_connection_stats': ('database', '固定连接聚合，不返回SQL、账号或客户端地址'),
}
LOG_TOOLS = {'linux.get_system_logs', 'docker.get_container_logs'}
ASSET_FIELDS = {'asset_id','kind','display_name','project_scope','parent_id','enabled','criticality',
                'environment','version','verified_at','connection_ref','identity_hash','capabilities'}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def asset_id(value):
    if not isinstance(value, str) or not re.fullmatch(ASSET_PATTERN, value):
        raise ValueError('invalid_args')
    return value


def schema(name):
    props = {'asset_id': {'type':'string', 'pattern':'^'+ASSET_PATTERN+'$'}}
    if name in LOG_TOOLS:
        props.update(limit={'type':'integer','minimum':1,'maximum':500,'default':100},
                     since_seconds={'type':'integer','minimum':1,'maximum':86400,'default':3600})
    return {'type':'object','properties':props,'required':['asset_id'],'additionalProperties':False}


def validate_args(name, arguments):
    if name not in TOOLS: raise ValueError('capability_unavailable' if name == 'postgresql.get_slow_queries' else 'guard_blocked')
    allowed = {'asset_id','limit','since_seconds'} if name in LOG_TOOLS else {'asset_id'}
    if not isinstance(arguments, dict) or set(arguments)-allowed or 'asset_id' not in arguments:
        raise ValueError('invalid_args')
    out = {'asset_id': asset_id(arguments['asset_id'])}
    if name in LOG_TOOLS:
        for key, default, maximum in (('limit',100,500), ('since_seconds',3600,86400)):
            value=arguments.get(key,default)
            if type(value) is not int or not 1 <= value <= maximum: raise ValueError('invalid_args')
            out[key]=value
    return out


def validate_catalog(assets):
    if not isinstance(assets,list) or not 1 <= len(assets) <= 200: raise ValueError('invalid_catalog')
    indexed={}
    for a in assets:
        if not isinstance(a,dict) or set(a)!=ASSET_FIELDS: raise ValueError('invalid_catalog')
        aid=asset_id(a['asset_id'])
        if aid in indexed or a['kind'] not in KINDS or a['project_scope']!='powersystem': raise ValueError('invalid_catalog')
        if type(a['version']) is not int or a['version']<1 or type(a['enabled']) is not bool: raise ValueError('invalid_catalog')
        if a['criticality'] not in ('core','support','drill') or a['environment'] not in ('production','acceptance','fixture'): raise ValueError('invalid_catalog')
        for k in ('display_name','connection_ref'):
            if not isinstance(a[k],str) or not 1<=len(a[k])<=100 or any(ord(c)<32 for c in a[k]): raise ValueError('invalid_catalog')
        asset_id(a['connection_ref'])
        if not re.fullmatch('[a-f0-9]{64}',a['identity_hash']): raise ValueError('invalid_catalog')
        if a['verified_at'] is not None:
            stamp=datetime.fromisoformat(a['verified_at'].replace('Z','+00:00'))
            if stamp.tzinfo is None: raise ValueError('invalid_catalog')
        if a['enabled'] and a['verified_at'] is None: raise ValueError('asset_not_verified')
        expected={t for t,(kind,_) in TOOLS.items() if kind==a['kind']}
        if a['kind']=='service' and a['criticality'] in ('support','drill') and a['environment'] in ('acceptance','fixture'):
            expected.update(('service.diagnose','service.logs','service.evidence','service.restart'))
        if a['kind']=='container' and a['criticality']=='drill' and a['environment'] in ('acceptance','fixture'):
            expected.add('docker.restart_drill_container')
        if not isinstance(a['capabilities'],list) or len(set(a['capabilities']))!=len(a['capabilities']) or not set(a['capabilities'])<=expected: raise ValueError('invalid_catalog')
        indexed[aid]=a
    for a in assets:
        parent=a['parent_id']
        if a['kind']=='host':
            if parent is not None: raise ValueError('invalid_catalog')
        elif parent not in indexed or indexed[parent]['kind']!='host': raise ValueError('invalid_catalog')
    return indexed


def public_asset(a):
    return {k:a[k] for k in ('asset_id','kind','display_name','parent_id','enabled','criticality',
                             'environment','version','verified_at','capabilities')}


def sign_grant(value,key):
    if len(key)<32: raise ValueError('invalid_grant_key')
    payload=base64.urlsafe_b64encode(canonical(value)).decode().rstrip('=')
    return payload+'.'+hmac.new(key.encode(),payload.encode(),hashlib.sha256).hexdigest()


GRANT_FIELDS={'aud','kid','iat','exp','task_id','trace_id','execution_epoch','step_no','call_id','user_id','role',
              'tool','args_hash','asset_id','asset_version','registry_hash','deadline'}


def verify_grant(token,key):
    try:
        if not isinstance(token,str) or len(token)>12000 or len(key)<32: raise ValueError()
        payload,signature=token.split('.')
        if not hmac.compare_digest(hmac.new(key.encode(),payload.encode(),hashlib.sha256).hexdigest(),signature): raise ValueError()
        value=json.loads(base64.urlsafe_b64decode(payload+'='*(-len(payload)%4)))
        if set(value)!=GRANT_FIELDS or value['aud']!='ma-executor' or value['kid']!='ma-exec-v1': raise ValueError()
        now=time.time()
        if any(type(value[k]) is not int for k in ('iat','exp','execution_epoch','step_no','user_id','asset_version')): raise ValueError()
        if value['iat']>now+1 or value['exp']<=now or not 0<value['exp']-value['iat']<=30: raise ValueError()
        if min(value[k] for k in ('execution_epoch','step_no','user_id','asset_version'))<1 or value['role'] not in ('operator','admin'): raise ValueError()
        if not isinstance(value['deadline'],(int,float)) or isinstance(value['deadline'],bool) or not math.isfinite(value['deadline']) or value['deadline']<=now: raise ValueError()
        for k in ('task_id','trace_id','call_id'): UUID(value[k])
        asset_id(value['asset_id'])
        for k in ('args_hash','registry_hash'):
            if not re.fullmatch('[a-f0-9]{64}',value[k]): raise ValueError()
        if value['tool'] not in TOOLS: raise ValueError()
        return value
    except (ValueError,TypeError,KeyError,AttributeError,UnicodeError):
        raise ValueError('invalid_execution_grant') from None


def validate_dispatch(request,key,registry):
    if not isinstance(request,dict) or set(request)!={'protocol','grant','tool','args'} or request['protocol']!='ma-exec/v1':
        raise ValueError('invalid_dispatch')
    grant=verify_grant(request['grant'],key)
    args=validate_args(request['tool'],request['args'])
    if grant['tool']!=request['tool'] or grant['args_hash']!=digest(args) or grant['asset_id']!=args['asset_id']:
        raise ValueError('context_mismatch')
    indexed=validate_catalog(registry['assets'])
    if grant['registry_hash']!=digest(registry['assets']): raise ValueError('asset_version_changed')
    asset=indexed.get(args['asset_id'])
    if not asset: raise ValueError('asset_not_found')
    if not asset['enabled'] or not asset['verified_at']: raise ValueError('asset_not_verified')
    if asset['version']!=grant['asset_version']: raise ValueError('asset_version_changed')
    if asset['kind']!=TOOLS[request['tool']][0] or request['tool'] not in asset['capabilities']: raise ValueError('capability_unavailable')
    parent=indexed.get(asset['parent_id']) if asset['parent_id'] else asset
    if not parent['enabled'] or not parent['verified_at']: raise ValueError('asset_not_verified')
    return grant,args,asset


# Only enumerated operational event identifiers leave the host; free-form error/message/query is discarded.
EVENTS={'api_started','gateway_started','gateway_subscribed','subscribe_failed','telemetry_rejected',
 'unregistered_device','device_lookup_failed','station_mismatch','queue_write_failed','queue_read_failed',
 'queue_decode_failed','tdengine_write_failed','queue_transaction_failed','queue_delete_failed','queue_commit_failed',
 'tdengine_batch_written','business_notification_worker_failed','business_notification_retry','device_table_deferred',
 'interpretation_scan_failed','monitor_consumer_unavailable','monitor_payload_invalid','monitor_persist_failed',
 'interpretation_claim_failed','prediction_devices_unavailable','prediction_skipped','backend_subscribe_failed',
 'backend_rejected','chat_admission_release_failed','service_started','service_stopped','service_failed',
 'connection_failed','health_check_failed'}


def safe_logs(text,limit=100,byte_truncated=False):
    rows=[]; filtered=0; lines=text.splitlines()
    for line in lines[-limit:]:
        try: item=json.loads(line)
        except (ValueError,TypeError): filtered+=1; continue
        if not isinstance(item,dict): filtered+=1; continue
        event=item.get('msg',item.get('event'))
        if not isinstance(event,str) or event not in EVENTS: filtered+=1; continue
        row={'event':event}
        level=item.get('level',item.get('PRIORITY'))
        if level in ('debug','info','warn','warning','error','fatal','0','1','2','3','4','5','6','7'): row['level']=level
        stamp=item.get('ts',item.get('timestamp'))
        if type(stamp) in (int,float) and math.isfinite(stamp): row['timestamp']=stamp
        elif isinstance(stamp,str):
            try:
                dt=datetime.fromisoformat(stamp.replace('Z','+00:00'))
                if dt.tzinfo: row['timestamp']=dt.isoformat()
            except ValueError: pass
        for field in ('count','attempt','duration_ms','status_code'):
            value=item.get(field)
            if type(value) in (int,float) and math.isfinite(value) and 0<=value<=10**12: row[field]=value
        rows.append(row)
    encoded=canonical(rows)
    while rows and len(encoded)>32768:
        rows.pop(0); filtered+=1; encoded=canonical(rows); byte_truncated=True
    return {'events':rows,'input_lines':min(len(lines),limit),'filtered_lines':filtered,
            'truncated':byte_truncated or len(lines)>limit,'complete':False,
            'filter_policy':'operational-events-v1; unknown text and user content omitted'}


COMMON_OUTPUT={'vantage','sampled_at','collected_at','truncated','complete'}
OUTPUT_FIELDS={
 'linux.get_cpu_usage':{'cpu_percent','load_1m','logical_cpus','sample_seconds'},
 'linux.get_memory_usage':{'total_bytes','available_bytes','used_bytes','swap_total_bytes','swap_free_bytes'},
 'linux.get_disk_status':{'filesystems'}, 'linux.list_services':{'list'},
 'docker.list_containers':{'list'},
 'docker.inspect_container':{'state','running','restart_count','image_digest','health','started_at','finished_at','exit_code'},
 'docker.get_container_stats':{'cpu_percent','memory_usage_bytes','memory_limit_bytes','memory_working_set_bytes','sample_seconds'},
 'postgresql.get_db_status':{'reachable','server_version','in_recovery','database_size_bytes','connections','commits','rollbacks','deadlocks'},
 'postgresql.get_connection_stats':{'connections','active','idle','idle_in_transaction','waiting','max_connections'},
}
for _log_tool in LOG_TOOLS:
    OUTPUT_FIELDS[_log_tool]={'events','input_lines','filtered_lines','filter_policy'}


def validate_result(name,reply):
    if set(reply)!={'status','data','error'} or reply['status'] not in ('ok','error') or not isinstance(reply['data'],dict): raise ValueError('invalid_result')
    if reply['status']=='error':
        err=reply['error']
        allowed={'guard_blocked','invalid_args','invalid_dispatch','invalid_execution_grant','context_mismatch','asset_version_changed',
                 'asset_not_found','asset_not_verified','capability_unavailable','dispatch_replayed','target_unreachable',
                 'invalid_result','timeout','collection_failed'}
        if reply['data'] or not isinstance(err,dict) or set(err)!={'code','message','retryable'} or err['code'] not in allowed or err['message']!=err['code'] or type(err['retryable']) is not bool:
            raise ValueError('invalid_result')
        return reply
    data=reply['data']
    if reply['error'] is not None or set(data)!=(COMMON_OUTPUT|OUTPUT_FIELDS[name]): raise ValueError('invalid_result')
    if data['vantage']!=('container' if name.startswith('docker.') and name!='docker.list_containers' else 'database' if name.startswith('postgresql.') else 'host'):
        raise ValueError('invalid_result')
    for field in ('sampled_at','collected_at'):
        stamp=datetime.fromisoformat(data[field].replace('Z','+00:00'))
        if stamp.tzinfo is None: raise ValueError('invalid_result')
    if type(data['truncated']) is not bool or type(data['complete']) is not bool: raise ValueError('invalid_result')
    numeric=OUTPUT_FIELDS[name]-{'filesystems','list','events','filter_policy','state','running','image_digest','health','started_at','finished_at','reachable','server_version','in_recovery'}
    for field in numeric:
        val=data[field]
        if val is None and field=='cpu_percent': continue
        if type(val) not in (int,float) or not math.isfinite(val) or val<0: raise ValueError('invalid_result')
    if name in LOG_TOOLS:
        if data['complete'] is not False or data['filter_policy']!='operational-events-v1; unknown text and user content omitted': raise ValueError('invalid_result')
        if not isinstance(data['events'],list) or len(data['events'])>500: raise ValueError('invalid_result')
        for row in data['events']:
            if not isinstance(row,dict) or set(row)-{'event','level','timestamp','count','attempt','duration_ms','status_code'} or row.get('event') not in EVENTS: raise ValueError('invalid_result')
            if row.get('level') not in (None,'debug','info','warn','warning','error','fatal','0','1','2','3','4','5','6','7'): raise ValueError('invalid_result')
            if 'timestamp' in row:
                val=row['timestamp']
                if type(val) in (int,float):
                    if not math.isfinite(val): raise ValueError('invalid_result')
                elif not isinstance(val,str) or datetime.fromisoformat(val.replace('Z','+00:00')).tzinfo is None: raise ValueError('invalid_result')
            for field in ('count','attempt','duration_ms','status_code'):
                if field in row and (type(row[field]) not in (int,float) or not math.isfinite(row[field]) or not 0<=row[field]<=10**12): raise ValueError('invalid_result')
        if len(canonical(data['events']))>32768: raise ValueError('invalid_result')
    if name=='linux.get_disk_status':
        for row in data['filesystems']:
            if set(row)!={'filesystem','total_bytes','available_bytes','free_bytes'} or not __import__('re').fullmatch(ASSET_PATTERN,row['filesystem']): raise ValueError('invalid_result')
            if any(type(row[k]) is not int or row[k]<0 for k in ('total_bytes','available_bytes','free_bytes')): raise ValueError('invalid_result')
    if name=='linux.list_services':
        for row in data['list']:
            if set(row)!={'asset_id','ActiveState','SubState','LoadState'}: raise ValueError('invalid_result')
            asset_id(row['asset_id'])
            if any(not isinstance(row[k],str) or not re.fullmatch('[a-z-]{1,40}',row[k]) for k in ('ActiveState','SubState','LoadState')): raise ValueError('invalid_result')
    if name in ('docker.inspect_container','docker.list_containers'):
        rows=data['list'] if name.endswith('list_containers') else [data]
        for row in rows:
            keys=OUTPUT_FIELDS['docker.inspect_container']|{'vantage','asset_id'} if name.endswith('list_containers') else COMMON_OUTPUT|OUTPUT_FIELDS[name]
            if set(row)!=keys: raise ValueError('invalid_result')
            if 'asset_id' in row: asset_id(row['asset_id'])
            if row['vantage']!='container' or row['state'] not in ('created','running','paused','restarting','removing','exited','dead') or row['health'] not in ('healthy','unhealthy','starting','not_configured'): raise ValueError('invalid_result')
            if type(row['running']) is not bool or type(row['restart_count']) is not int or type(row['exit_code']) is not int: raise ValueError('invalid_result')
            if not re.fullmatch('sha256:[a-f0-9]{64}',row['image_digest']): raise ValueError('invalid_result')
            for field in ('started_at','finished_at'):
                if not isinstance(row[field],str) or not re.fullmatch('[0-9TZ:.+-]{1,40}',row[field]): raise ValueError('invalid_result')
    if name.startswith('postgresql.'):
        if name.endswith('get_db_status') and (type(data['reachable']) is not bool or type(data['in_recovery']) is not bool or not re.fullmatch(r'[0-9]+\.[0-9]+(?:\.[0-9]+)?(?: \([A-Za-z0-9.+ _~-]+\))?',data['server_version'])): raise ValueError('invalid_result')
    return reply
