ALTER TABLE devices ADD COLUMN IF NOT EXISTS remark TEXT NOT NULL DEFAULT '';
ALTER TABLE devices ADD COLUMN IF NOT EXISTS remark_version BIGINT NOT NULL DEFAULT 0 CHECK (remark_version >= 0);
ALTER TABLE devices ADD COLUMN IF NOT EXISTS remark_updated_by BIGINT NULL;
ALTER TABLE devices ADD COLUMN IF NOT EXISTS remark_updated_at TIMESTAMPTZ NULL;
CREATE TABLE IF NOT EXISTS business_action_receipts (
 execution_ref UUID PRIMARY KEY,
 owner_id BIGINT NOT NULL,
 run_id UUID NOT NULL,
 action_id UUID NOT NULL,
 capability TEXT NOT NULL,
 args_hash TEXT NOT NULL,
 target_version TEXT NOT NULL,
 result JSONB NOT NULL,
 before_hash TEXT NOT NULL,
 after_hash TEXT NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_business_action_receipts_owner ON business_action_receipts(owner_id);
CREATE INDEX IF NOT EXISTS idx_business_action_receipts_run ON business_action_receipts(run_id);
