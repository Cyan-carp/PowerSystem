"""Async LangGraph saver in the RAG PostgreSQL namespace, fenced by the run lease."""
import json
from uuid import UUID
from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple, WRITES_IDX_MAP
from .run_store import encode


class RunCheckpointer(BaseCheckpointSaver):
    def __init__(self,store,rid,worker,epoch,redact=None):
        super().__init__()
        self.store,self.rid,self.worker,self.epoch=store,rid,worker,epoch
        self.redact=redact or (lambda text:text)

    def sanitized(self,value):
        if isinstance(value,str):return self.redact(value)
        if isinstance(value,list):return [self.sanitized(v) for v in value]
        if isinstance(value,tuple):return tuple(self.sanitized(v) for v in value)
        if isinstance(value,dict):return {k:self.sanitized(v) for k,v in value.items() if k not in ('reasoning_content','chain_of_thought')}
        return value

    def scope(self,config):
        value=config['configurable']
        if UUID(value['thread_id'])!=self.rid:raise ValueError('checkpoint_thread_mismatch')
        return value.get('checkpoint_ns','')

    async def aget_tuple(self,config):
        ns=self.scope(config)
        cid=config['configurable'].get('checkpoint_id')
        row=await self.store.pool.fetchrow('SELECT * FROM assistant_checkpoints WHERE run_id=$1 AND ns=$2 AND ($3::text IS NULL OR checkpoint_id=$3) ORDER BY checkpoint_id DESC LIMIT 1',self.rid,ns,cid)
        if not row:return None
        writes=await self.store.pool.fetch('SELECT * FROM assistant_checkpoint_writes WHERE run_id=$1 AND ns=$2 AND checkpoint_id=$3 ORDER BY task_id,idx',self.rid,ns,row['checkpoint_id'])
        conf=dict(configurable=dict(thread_id=str(self.rid),checkpoint_ns=ns,checkpoint_id=row['checkpoint_id']))
        parent=dict(configurable=dict(thread_id=str(self.rid),checkpoint_ns=ns,checkpoint_id=row['parent_id'])) if row['parent_id'] else None
        return CheckpointTuple(conf,self.serde.loads_typed((row['checkpoint_type'],row['checkpoint'])),json.loads(row['metadata']),parent,
                               [(w['task_id'],w['channel'],self.serde.loads_typed((w['value_type'],w['value']))) for w in writes])

    async def aput(self,config,checkpoint,metadata,new_versions):
        ns=self.scope(config)
        kind,raw=self.serde.dumps_typed(self.sanitized(checkpoint))
        async with self.store.pool.acquire() as c,c.transaction():
            await self.store.locked(c,self.rid,self.worker,self.epoch)
            await c.execute('''INSERT INTO assistant_checkpoints(run_id,ns,checkpoint_id,parent_id,checkpoint_type,checkpoint,metadata)
                VALUES($1,$2,$3,$4,$5,$6,$7::jsonb) ON CONFLICT DO NOTHING''',self.rid,ns,checkpoint['id'],config['configurable'].get('checkpoint_id'),kind,raw,encode(metadata))
        return dict(configurable=dict(thread_id=str(self.rid),checkpoint_ns=ns,checkpoint_id=checkpoint['id']))

    async def aput_writes(self,config,writes,task_id,task_path=''):
        ns=self.scope(config)
        async with self.store.pool.acquire() as c,c.transaction():
            await self.store.locked(c,self.rid,self.worker,self.epoch)
            for idx,(channel,value) in enumerate(writes):
                kind,raw=self.serde.dumps_typed(self.sanitized(value))
                await c.execute('''INSERT INTO assistant_checkpoint_writes(run_id,ns,checkpoint_id,task_id,idx,channel,value_type,value)
                   VALUES($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT(run_id,ns,checkpoint_id,task_id,idx) DO NOTHING''',
                   self.rid,ns,config['configurable']['checkpoint_id'],task_id,WRITES_IDX_MAP.get(channel,idx),channel,kind,raw)

    async def alist(self,config,*,filter=None,before=None,limit=None):
        ns=self.scope(config)
        rows=await self.store.pool.fetch('SELECT checkpoint_id FROM assistant_checkpoints WHERE run_id=$1 AND ns=$2 ORDER BY checkpoint_id DESC LIMIT $3',self.rid,ns,limit or 100)
        for row in rows:
            item=await self.aget_tuple(dict(configurable=dict(thread_id=str(self.rid),checkpoint_ns=ns,checkpoint_id=row['checkpoint_id'])))
            if (not before or row['checkpoint_id']<before['configurable']['checkpoint_id']) and (not filter or all(item.metadata.get(k)==v for k,v in filter.items())):yield item
