ALTER TABLE rag_sessions ADD COLUMN IF NOT EXISTS archived boolean NOT NULL DEFAULT false;
