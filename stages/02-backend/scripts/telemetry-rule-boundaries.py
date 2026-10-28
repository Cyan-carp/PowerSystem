"""Generate boundary cases for the actual TR-S01 Prometheus rules."""
import json
import sys
from pathlib import Path

rules = str(Path(__file__).resolve().parents[2] / '04-frontend/deploy/alerts.yml')
base = {
 'start_timestamp_seconds':'-100+0x8', 'probe_pg_ok':'1+0x8', 'probe_redis_ok':'1+0x8',
 'probe_timestamp_seconds':'0+15x8', 'inbox_receive_age_seconds':'120+0x8',
 'business_success_age_seconds':'120+0x8', 'queue_oldest_age_seconds':'120+0x8',
 'expected_devices':'2+0x8', 'fresh_ratio':'0+0x8', 'missing_keys':'2+0x8',
 'cache_received_worst_age_seconds':'120+0x8', 'cache_sample_worst_age_seconds':'120+0x8',
 'maintenance':'0+0x8', 'maintenance_known':'1+0x8', 'maintenance_age_seconds':'0+0x8',
 'classification_known':'1+0x8', 'loop_timestamp_seconds':'0+0x8', 'redis_write_timestamp_seconds':'0+0x8'}

def case(name, changes, expressions, at='60s'):
    values=base | changes
    series=[{'series': f'powersystem_telemetry_{key}{{instance="fixture",job="api"}}', 'values':value}
            for key,value in values.items() if value is not None]
    series.append({'series':'up{instance="fixture",job="api"}', 'values':'1+0x8'})
    tests=[]
    for alert, expected in expressions.items():
        tests.append({'expr':f'(count(ALERTS{{alertname="PowerSystemTelemetry{alert}",alertstate="firing"}}) or vector(0))',
                      'eval_time':at,'exp_samples':[{'labels':'{}','value':expected}]})
    return {'name':name, 'interval':'15s', 'input_series':series, 'promql_expr_test':tests}

noise={k:0 for k in ('InputStopped','CoverageLow','WorkerStalled','NoBusinessSuccess','RedisWriteStalled')}
tests=[case('explicit idle',{'expected_devices':'0+0x8'},noise),
       case('planned maintenance',{'maintenance':'1+0x8'},noise),
       case('overdue maintenance',{'maintenance':'1+0x8','maintenance_age_seconds':'3600+0x8'},noise|{'MaintenanceOverdue':1}),
       case('missing singleton remains visible',{'maintenance':'NaN NaN NaN NaN NaN NaN NaN NaN NaN','maintenance_known':'0+0x8'}, {'InputStopped':1,'RegistryUnknown':1,'ProbeFailed':0}),
       case('startup grace',{'start_timestamp_seconds':'0+0x8','probe_timestamp_seconds':'0+0x8'},noise|{'ProbeFailed':0},at='15s'),
       case('probe metric missing per instance',{'probe_timestamp_seconds':None},{'ProbeFailed':1}),
       case('receive never initialized',{'inbox_receive_age_seconds':'NaN NaN NaN NaN NaN NaN NaN NaN NaN'}, {'InputStopped':1})]
Path(sys.argv[1]).write_text(json.dumps({'rule_files':[rules],'evaluation_interval':'15s','tests':tests},indent=2))
