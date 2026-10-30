-- CLI chat sessions, turns, and memory extraction audit

CREATE TABLE IF NOT EXISTS chat_sessions (
  id              UUID PRIMARY KEY,
  subject_id      UUID NOT NULL REFERENCES subjects(id),
  actor_id        UUID NOT NULL REFERENCES actors(id),
  channel         TEXT NOT NULL DEFAULT 'cli',
  source_id       UUID REFERENCES sources(id),
  started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_activity_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  metadata        JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS chat_sessions_subject_idx ON chat_sessions (subject_id, last_activity_at DESC);

CREATE TABLE IF NOT EXISTS chat_turns (
  id                 UUID PRIMARY KEY,
  session_id         UUID NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
  turn_index         INT NOT NULL,
  role               TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
  content            TEXT NOT NULL,
  content_hash       TEXT NOT NULL,
  evidence_record_id UUID REFERENCES evidence_records(id),
  created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (session_id, turn_index)
);
CREATE INDEX IF NOT EXISTS chat_turns_session_idx ON chat_turns (session_id, turn_index);

CREATE TABLE IF NOT EXISTS memory_extraction_runs (
  id              UUID PRIMARY KEY,
  session_id      UUID NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
  turn_id         UUID NOT NULL REFERENCES chat_turns(id) ON DELETE CASCADE,
  extractor       TEXT NOT NULL,
  prompt_version  TEXT NOT NULL,
  raw_json        JSONB,
  decision        TEXT NOT NULL CHECK (decision IN ('saved', 'skipped', 'failed')),
  skip_reason     TEXT,
  memory_id       UUID REFERENCES memories(id),
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS memory_extraction_runs_turn_idx ON memory_extraction_runs (turn_id);
