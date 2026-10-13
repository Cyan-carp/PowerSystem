ALTER TABLE rag_turns ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'chat';
ALTER TABLE rag_turns ADD COLUMN IF NOT EXISTS parent_turn_id bigint REFERENCES rag_turns(id) ON DELETE CASCADE;
ALTER TABLE rag_turns ADD COLUMN IF NOT EXISTS verification_id uuid;
CREATE UNIQUE INDEX IF NOT EXISTS rag_turns_verification_unique ON rag_turns(session_id, verification_id) WHERE verification_id IS NOT NULL;
