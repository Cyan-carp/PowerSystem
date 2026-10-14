CREATE TABLE IF NOT EXISTS ma_assets (
 asset_id text PRIMARY KEY,
 kind text NOT NULL CHECK(kind IN ('host','service','container','database')),
 display_name text NOT NULL,
 project_scope text NOT NULL CHECK(project_scope='powersystem'),
 parent_id text REFERENCES ma_assets(asset_id),
 enabled boolean NOT NULL,
 criticality text NOT NULL CHECK(criticality IN ('core','support','drill')),
 environment text NOT NULL CHECK(environment IN ('production','acceptance','fixture')),
 version integer NOT NULL CHECK(version>0),
 verified_at timestamptz,
 connection_ref text NOT NULL,
 identity_hash text NOT NULL,
 capabilities jsonb NOT NULL,
 registry_hash text NOT NULL,
 updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS ma_asset_versions (
 asset_id text NOT NULL REFERENCES ma_assets(asset_id),
 version integer NOT NULL,
 snapshot jsonb NOT NULL,
 imported_by bigint NOT NULL,
 imported_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(asset_id,version)
);
ALTER TABLE ma_task_steps ADD COLUMN IF NOT EXISTS asset_id text;
ALTER TABLE ma_task_steps ADD COLUMN IF NOT EXISTS asset_version integer;
ALTER TABLE ma_tool_calls ADD COLUMN IF NOT EXISTS asset_id text;
ALTER TABLE ma_tool_calls ADD COLUMN IF NOT EXISTS asset_version integer;
ALTER TABLE ma_tool_calls ADD COLUMN IF NOT EXISTS adapter_claimed_at timestamptz;
