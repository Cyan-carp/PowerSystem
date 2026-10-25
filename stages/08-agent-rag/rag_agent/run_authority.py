"""Read/cancel adapter for the existing stage9 controller; never executes an action."""
from datetime import datetime,timezone
from uuid import UUID


class Stage9Authority:
    def __init__(self,ops):self.ops=ops

    async def inspect(self,row,cancel=False):
        projections=[];pending=False;effects=False
        for item in row['actions']:
            tid=str(UUID(item['task_id']))
            if item.get('capability') in ('alarm.ack','device.remark','device.remark.read') or str(item.get('capability','')).startswith(('service.','schedule.')) or item.get('error_code'):
                path=('/internal/ma-schedule/v1/actions/' if str(item.get('capability','')).startswith('schedule.') else '/internal/ma-ops/v1/actions/' if str(item.get('capability','')).startswith('service.') else '/internal/ma-action/v1/actions/')+tid
                value=await self.ops.request('POST',path+'/cancel',{}) if cancel else await self.ops.request('GET',path)
                if value.get('run_id')!=str(row['id']):raise ValueError('authority_binding_changed')
                projections.append(value)
                pending|=value['status'] in ('started','execution_uncertain','unknown','pending','approved')
                effects|=(value['status']=='succeeded' and value.get('capability') not in ('device.remark.read','service.diagnose','service.logs','service.evidence') or value['status']=='failed' and not value.get('no_effect',False))
                continue
            if cancel:await self.ops.request('POST','/internal/ma/v1/tasks/'+tid+'/cancel',{})
            report=await self.ops.get(tid)
            attempts={a['approval_id']:a for a in report.get('execution_attempts',[])}
            approval=next((a for a in report.get('approvals',[]) if a['approval_id']==item.get('approval_id')),None)
            attempt=attempts.get(item.get('approval_id'))
            state=attempt['status'] if attempt else approval['status'] if approval else 'unknown'
            uncertain=state in ('started','execution_uncertain','unknown','pending','approved')
            pending|=uncertain
            # A failed issued action can have side effects too. Only explicit no_effect evidence clears it.
            effects|=bool(attempt and (state=='succeeded' or state=='failed' and not (attempt.get('evidence') or {}).get('no_effect',False)))
            projections.append({k:v for k,v in {**item,'status':state,'observed_at':datetime.now(timezone.utc).isoformat(),'stale':False}.items()
                                if k in ('action_id','task_id','approval_id','plan_hash','status','authority_revision','observed_at','stale','execution_ref','effect_summary','target_summary','expires_at','risk_class')})
        return dict(pending=pending,effects=effects,actions=projections)
