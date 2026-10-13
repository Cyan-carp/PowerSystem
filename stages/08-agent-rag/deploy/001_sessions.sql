CREATE TABLE IF NOT EXISTS rag_sessions (
  id uuid PRIMARY KEY,
  user_id bigint NOT NULL CHECK (user_id > 0),
  title text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS rag_sessions_user_updated ON rag_sessions(user_id, updated_at DESC);
CREATE TABLE IF NOT EXISTS rag_turns (
  id bigserial PRIMARY KEY,
  session_id uuid NOT NULL REFERENCES rag_sessions(id) ON DELETE CASCADE,
  question text NOT NULL,
  response jsonb NOT NULL,
  model_messages jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS rag_turns_session_id ON rag_turns(session_id, id);
