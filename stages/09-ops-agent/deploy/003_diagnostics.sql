-- Run once by the owner in the registered project business database, never by the model.
-- Login role/password provisioned separately from a private file, without SQL/log output.
CREATE SCHEMA IF NOT EXISTS ma_ops_monitor AUTHORIZATION ma_ops_monitor_owner;
REVOKE ALL ON SCHEMA ma_ops_monitor FROM PUBLIC;
GRANT USAGE ON SCHEMA ma_ops_monitor TO ma_ops_diagnostics;
CREATE OR REPLACE FUNCTION ma_ops_monitor.db_status() RETURNS jsonb
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog AS $$
 SELECT jsonb_build_object('reachable',true,'server_version',current_setting('server_version'),
   'in_recovery',pg_is_in_recovery(),'database_size_bytes',pg_database_size(current_database()),
   'connections',numbackends,'commits',xact_commit,'rollbacks',xact_rollback,'deadlocks',deadlocks)
 FROM pg_stat_database WHERE datname=current_database();
$$;
CREATE OR REPLACE FUNCTION ma_ops_monitor.connection_stats() RETURNS jsonb
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog AS $$
 SELECT jsonb_build_object('connections',count(*),
   'active',count(*) FILTER(WHERE state='active'),
   'idle',count(*) FILTER(WHERE state='idle'),
   'idle_in_transaction',count(*) FILTER(WHERE state='idle in transaction'),
   'waiting',count(*) FILTER(WHERE wait_event_type IS NOT NULL),
   'max_connections',current_setting('max_connections')::integer)
 FROM pg_stat_activity WHERE datname=current_database();
$$;
ALTER FUNCTION ma_ops_monitor.db_status() OWNER TO ma_ops_monitor_owner;
ALTER FUNCTION ma_ops_monitor.connection_stats() OWNER TO ma_ops_monitor_owner;
REVOKE ALL ON FUNCTION ma_ops_monitor.db_status(),ma_ops_monitor.connection_stats() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ma_ops_monitor.db_status(),ma_ops_monitor.connection_stats() TO ma_ops_diagnostics;
