"""Publish a reviewed S07 observation via the S05 owner-bound artifact store."""
import hashlib,json,os
from uuid import uuid4,uuid5
from .artifacts import metadata
from .run_store import encode

async def publish(service,row,worker,actions):
    public=[]
    for a in actions:
        if a.get('capability')!='service.evidence' or a['status']!='succeeded':continue
        aid=uuid5(row['id'],'ops-evidence:'+a['action_id']);call='ops-evidence:'+a['action_id']
        old=await service.pool.fetchrow('SELECT *,id AS artifact_id FROM assistant_artifacts WHERE id=$1',aid)
        if old:public.append(metadata(old));continue
        observation=json.loads(a['display_result'])
        raw=json.dumps(dict(protocol='ops-evidence/v1',run_id=str(row['id']),action_id=a['action_id'],execution_ref=a['execution_ref'],policy_version=a['policy_version'],observation=observation,limits='过滤后有界观察，不包含日志原文；不证明没有异常，不代表生产验收'),ensure_ascii=False,indent=2).encode('utf8')
        if len(raw)>262144:raise ValueError('ops_evidence_limit')
        key=str(uuid4());service.root.mkdir(parents=True,exist_ok=True);target=service.path(key);temp=target.with_suffix('.'+str(worker)+'.tmp')
        try:
            temp.write_bytes(raw);os.replace(temp,target)
            async with service.pool.acquire() as c,c.transaction():
                current=await service.runs.locked(c,row['id'],worker,row['lease_epoch'])
                if current['cancel_requested']:raise ValueError('evidence_cancelled')
                saved=await c.fetchrow("INSERT INTO assistant_artifacts(id,run_id,user_id,call_id,name,mime_type,storage_key,size,sha256,status,provenance) VALUES($1,$2,$3,$4,'service-evidence.json','application/json',$5,$6,$7,'ready',$8::jsonb) RETURNING *,id AS artifact_id",aid,row['id'],row['user_id'],call,key,len(raw),hashlib.sha256(raw).hexdigest(),encode(dict(source='stage9-reviewed-service-observation',action_id=a['action_id'],policy=a['policy_version'],model_content=False)))
                item=metadata(saved);current['artifacts'].append(item);await service.runs._event(c,current,'artifact_ready',item);public.append(item)
        except BaseException:
            registered=await service.pool.fetchval('SELECT EXISTS(SELECT 1 FROM assistant_artifacts WHERE storage_key=$1)',key)
            if not registered:target.unlink(missing_ok=True)
            temp.unlink(missing_ok=True);raise
    return public
