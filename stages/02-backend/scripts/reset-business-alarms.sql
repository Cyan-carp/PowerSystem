-- Run only through the release driver after backup and maintenance drain.
-- psql variables: cutoff = frozen MAX(alarm_records.id). No identity reset.
\set ON_ERROR_STOP on
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='20s';
SELECT pg_advisory_xact_lock(214021,5);
LOCK TABLE alarm_records,agent_interpretations,agent_interpretation_reads,business_notification_outbox
 IN ACCESS EXCLUSIVE MODE;
DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM alarm_maintenance_state WHERE id=1 AND enabled) THEN
  RAISE EXCEPTION 'maintenance_required';
 END IF;
END $$;
CREATE TEMP TABLE reset_alarm_targets ON COMMIT DROP AS
 SELECT id FROM alarm_records WHERE id<=:'cutoff'::bigint;
-- Fail rather than silently leave newly-created business alarms behind.
SELECT set_config('powersystem.reset_cutoff',:'cutoff',true);
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM alarm_records WHERE id>current_setting('powersystem.reset_cutoff')::bigint) THEN
  RAISE EXCEPTION 'alarm_writes_not_drained';
 END IF;
END $$;
DELETE FROM business_notification_outbox n USING reset_alarm_targets a
 WHERE n.payload->>'alarm_id'=a.id::text OR n.event_key LIKE 'alarm:'||a.id::text||':%';
DELETE FROM agent_interpretations i USING reset_alarm_targets a WHERE i.alarm_id=a.id;
DELETE FROM alarm_records a USING reset_alarm_targets t WHERE a.id=t.id;
INSERT INTO agent_scan_cursor(name,baseline_id,last_id) VALUES('alarms',:'cutoff'::bigint,:'cutoff'::bigint)
 ON CONFLICT(name) DO UPDATE SET baseline_id=EXCLUDED.baseline_id,last_id=EXCLUDED.last_id;
COMMIT;
