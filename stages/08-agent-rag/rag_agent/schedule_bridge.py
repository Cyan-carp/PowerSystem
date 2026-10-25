"""Narrow grant attestation for the business API, with no cross-database SQL.

This endpoint has a distinct service credential and cannot create/approve grants,
accept an owner or role, or invoke any business handler. The API rechecks the
current user in its own database before issuing a fixed read.
"""
from datetime import datetime
from uuid import UUID
import json
from fastapi import HTTPException
from .run_store import encode

def routes(app):
    @app.get('/internal/schedule-bridge/v1/grants/{sid}')
    async def attest(sid:UUID,grant_id:UUID,kind:str,scheduled_for:datetime|None=None):
        svc=app.state.schedules
        await svc.guard()
        if kind not in ('check','energy','alarms') or (kind!='check' and (scheduled_for is None or scheduled_for.tzinfo is None)):
            raise HTTPException(400,'invalid_bridge_read')
        async with svc.pool.acquire() as c,c.transaction():
            g=await c.fetchrow("""SELECT s.user_id,s.actor_role,s.config_version,s.config::text AS config,
                s.scope_hash,g.grant_id::text AS grant_id,g.scope_hash AS grant_scope,
                g.config_version AS grant_version,g.destination_id,g.destination_version
                FROM assistant_schedules s JOIN assistant_schedule_grants g ON g.schedule_id=s.id
                WHERE s.id=$1 AND g.grant_id=$2 AND s.status='active' AND g.revoked_at IS NULL
                AND g.expires_at>now() AND g.user_id=s.user_id AND g.approved_by=s.user_id""",sid,grant_id)
            if not g:raise HTTPException(403,'schedule_grant_inactive')
            if kind!='check':
                slot=await c.fetchrow('''SELECT t.run_id FROM assistant_schedule_slots t
                    JOIN assistant_runs r ON r.id=t.run_id
                    WHERE t.schedule_id=$1 AND t.grant_id=$2 AND t.scheduled_for=$3
                    AND t.config_version=$4 AND r.user_id=$5''',sid,grant_id,scheduled_for,g['config_version'],g['user_id'])
                if not slot:raise HTTPException(403,'unclaimed_schedule_slot')
            # The one-second permission poll is not a read/delivery event.
            # Revocation/pausing already has its own durable schedule event.
            if kind!='check':
                await c.execute("INSERT INTO assistant_schedule_events(schedule_id,user_id,kind,detail) VALUES($1,$2,'bridge_attested',$3::jsonb)",sid,g['user_id'],encode(dict(capability=kind,grant_id=str(grant_id))))
            # Names deliberately match the closed Go DTO. No auth material leaves this route.
            return dict(g)
