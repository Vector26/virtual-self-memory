-- Agent-owned page enrichments (summary, kind, labels). Import must use these, not heuristics.

CREATE TABLE IF NOT EXISTS page_enrichments (
  id              UUID PRIMARY KEY,
  subject_id      UUID NOT NULL REFERENCES subjects(id),
  rel_path        TEXT NOT NULL,
  content_hash    TEXT NOT NULL,
  summary         TEXT NOT NULL,
  memory_kind     TEXT NOT NULL,
  labels          JSONB NOT NULL DEFAULT '[]'::jsonb,
  producer        TEXT NOT NULL,
  model           TEXT,
  prompt_version  TEXT NOT NULL,
  confidence      REAL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (subject_id, rel_path, content_hash)
);

CREATE INDEX IF NOT EXISTS page_enrichments_subject_path_idx
  ON page_enrichments (subject_id, rel_path);

CREATE TABLE IF NOT EXISTS personality_synthesis_runs (
  id              UUID PRIMARY KEY,
  profile_id      UUID NOT NULL REFERENCES personality_profiles(id),
  producer        TEXT NOT NULL,
  model           TEXT,
  prompt_version  TEXT NOT NULL,
  facet_snapshot  JSONB NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
