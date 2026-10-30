-- Personal data archive pipeline (Takeout / Meta inbox)

CREATE TABLE IF NOT EXISTS archive_files (
  id              UUID PRIMARY KEY,
  subject_id      UUID REFERENCES subjects(id),
  provider        TEXT NOT NULL,
  file_name       TEXT NOT NULL,
  file_path       TEXT,
  size_bytes      BIGINT NOT NULL DEFAULT 0,
  sha256          TEXT,
  state           TEXT NOT NULL DEFAULT 'discovered'
    CHECK (state IN (
      'discovered', 'stable', 'downloaded', 'unpacked', 'parsed',
      'analyzed', 'promoted', 'done', 'failed'
    )),
  error_message   TEXT,
  meta            JSONB NOT NULL DEFAULT '{}',
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (provider, file_name)
);

CREATE INDEX IF NOT EXISTS archive_files_state_idx ON archive_files (state);
CREATE INDEX IF NOT EXISTS archive_files_sha256_idx ON archive_files (sha256) WHERE sha256 IS NOT NULL;

CREATE TABLE IF NOT EXISTS archive_runs (
  id              UUID PRIMARY KEY,
  run_type        TEXT NOT NULL,
  subject_id      UUID REFERENCES subjects(id),
  archive_file_id UUID REFERENCES archive_files(id),
  state           TEXT NOT NULL DEFAULT 'running'
    CHECK (state IN ('running', 'completed', 'failed')),
  stats           JSONB NOT NULL DEFAULT '{}',
  started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  finished_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS archive_runs_type_idx ON archive_runs (run_type, started_at DESC);

CREATE TABLE IF NOT EXISTS archive_llm_runs (
  id              UUID PRIMARY KEY,
  archive_run_id  UUID REFERENCES archive_runs(id),
  layer           TEXT NOT NULL,
  prompt_version  TEXT NOT NULL,
  input_ref       JSONB,
  output_json     JSONB,
  verdict         TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS archive_llm_runs_run_idx ON archive_llm_runs (archive_run_id);

CREATE TABLE IF NOT EXISTS archive_personality_proposals (
  id                  UUID PRIMARY KEY,
  subject_id          UUID NOT NULL REFERENCES subjects(id),
  content             TEXT NOT NULL,
  evidence_event_ids  JSONB NOT NULL DEFAULT '[]',
  confidence          REAL,
  status              TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'approved', 'rejected')),
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
