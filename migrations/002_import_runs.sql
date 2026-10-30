CREATE TABLE IF NOT EXISTS import_runs (
  id          UUID PRIMARY KEY,
  subject_id  UUID NOT NULL REFERENCES subjects(id),
  root_path   TEXT NOT NULL,
  dry_run     BOOLEAN NOT NULL DEFAULT TRUE,
  stats       JSONB NOT NULL DEFAULT '{}',
  started_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  finished_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS import_runs_subject_idx ON import_runs (subject_id, started_at DESC);
