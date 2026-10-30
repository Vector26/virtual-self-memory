-- Persona pipeline phase 1 (part B): modes, evidence, proposals, voice tables
-- See docs/PERSONA_PIPELINE_PLAN.md §4 and VIRTUAL_SELF_ARCHITECTURE_PLAN.md §6.5

-- ---------------------------------------------------------------------------
-- Modes (versioned, chapter-scoped)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS persona_modes (
  id                  UUID PRIMARY KEY,
  subject_id          UUID NOT NULL REFERENCES subjects(id),
  name                TEXT NOT NULL,
  role_summary        TEXT NOT NULL DEFAULT '',
  triggers            JSONB NOT NULL DEFAULT '[]',
  voice_delta         JSONB NOT NULL DEFAULT '{}',
  values_delta        JSONB NOT NULL DEFAULT '{}',
  status              TEXT NOT NULL DEFAULT 'active'
    CHECK (status IN ('active', 'aspirational', 'retired')),
  introduced_at       TIMESTAMPTZ,
  retired_at          TIMESTAMPTZ,
  chapter_valid_from  TIMESTAMPTZ,
  chapter_valid_to    TIMESTAMPTZ,
  current_version_id  UUID,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (subject_id, name)
);

CREATE TABLE IF NOT EXISTS persona_mode_versions (
  id                    UUID PRIMARY KEY,
  mode_id               UUID NOT NULL REFERENCES persona_modes(id) ON DELETE CASCADE,
  version               INT NOT NULL,
  role_summary          TEXT NOT NULL,
  triggers              JSONB NOT NULL DEFAULT '[]',
  voice_delta           JSONB NOT NULL DEFAULT '{}',
  values_delta          JSONB NOT NULL DEFAULT '{}',
  status                TEXT NOT NULL DEFAULT 'active'
    CHECK (status IN ('active', 'superseded', 'draft')),
  approved_by           UUID REFERENCES actors(id),
  approved_at           TIMESTAMPTZ,
  supersedes_version_id UUID REFERENCES persona_mode_versions(id),
  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (mode_id, version)
);

ALTER TABLE persona_modes
  DROP CONSTRAINT IF EXISTS persona_modes_current_version_fk;
ALTER TABLE persona_modes
  ADD CONSTRAINT persona_modes_current_version_fk
  FOREIGN KEY (current_version_id) REFERENCES persona_mode_versions(id);

CREATE INDEX IF NOT EXISTS persona_modes_subject_idx
  ON persona_modes (subject_id, status);
CREATE INDEX IF NOT EXISTS persona_mode_versions_mode_idx
  ON persona_mode_versions (mode_id, version DESC);

CREATE TABLE IF NOT EXISTS persona_mode_evidence (
  id                  UUID PRIMARY KEY,
  mode_version_id     UUID NOT NULL REFERENCES persona_mode_versions(id) ON DELETE CASCADE,
  authored_text_id    UUID REFERENCES authored_texts(id) ON DELETE CASCADE,
  memory_version_id   UUID REFERENCES memory_versions(id) ON DELETE CASCADE,
  evidence_record_id  UUID REFERENCES evidence_records(id) ON DELETE CASCADE,
  event_id            UUID,
  relationship        TEXT NOT NULL
    CHECK (relationship IN ('supports', 'contradicts', 'illustrates')),
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CHECK (
    authored_text_id IS NOT NULL
    OR memory_version_id IS NOT NULL
    OR evidence_record_id IS NOT NULL
    OR event_id IS NOT NULL
  )
);
CREATE INDEX IF NOT EXISTS persona_mode_evidence_version_idx
  ON persona_mode_evidence (mode_version_id);

-- ---------------------------------------------------------------------------
-- Facet + proposal evidence lineage
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS personality_facet_evidence (
  id                  UUID PRIMARY KEY,
  facet_version_id    UUID NOT NULL REFERENCES personality_facet_versions(id) ON DELETE CASCADE,
  authored_text_id    UUID REFERENCES authored_texts(id) ON DELETE CASCADE,
  memory_version_id   UUID REFERENCES memory_versions(id) ON DELETE CASCADE,
  evidence_record_id  UUID REFERENCES evidence_records(id) ON DELETE CASCADE,
  event_id            UUID,
  relationship        TEXT NOT NULL
    CHECK (relationship IN ('supports', 'contradicts', 'illustrates')),
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CHECK (
    authored_text_id IS NOT NULL
    OR memory_version_id IS NOT NULL
    OR evidence_record_id IS NOT NULL
    OR event_id IS NOT NULL
  )
);
CREATE INDEX IF NOT EXISTS personality_facet_evidence_facet_idx
  ON personality_facet_evidence (facet_version_id);

CREATE TABLE IF NOT EXISTS personality_proposals (
  id                UUID PRIMARY KEY,
  profile_id        UUID NOT NULL REFERENCES personality_profiles(id) ON DELETE CASCADE,
  facet_type        TEXT NOT NULL,
  proposed_content  TEXT NOT NULL,
  reason            TEXT,
  confidence        REAL
    CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
  proposed_by       UUID REFERENCES actors(id),
  origin_mode       TEXT,
  status            TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'approved', 'rejected', 'expired')),
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  decided_at        TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS personality_proposals_profile_idx
  ON personality_proposals (profile_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS personality_proposal_evidence (
  id                  UUID PRIMARY KEY,
  proposal_id         UUID NOT NULL REFERENCES personality_proposals(id) ON DELETE CASCADE,
  authored_text_id    UUID REFERENCES authored_texts(id) ON DELETE CASCADE,
  memory_version_id   UUID REFERENCES memory_versions(id) ON DELETE CASCADE,
  evidence_record_id  UUID REFERENCES evidence_records(id) ON DELETE CASCADE,
  event_id            UUID,
  relationship        TEXT NOT NULL
    CHECK (relationship IN ('supports', 'contradicts', 'illustrates')),
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CHECK (
    authored_text_id IS NOT NULL
    OR memory_version_id IS NOT NULL
    OR evidence_record_id IS NOT NULL
    OR event_id IS NOT NULL
  )
);
CREATE INDEX IF NOT EXISTS personality_proposal_evidence_proposal_idx
  ON personality_proposal_evidence (proposal_id);

-- ---------------------------------------------------------------------------
-- Voice: stylometry, exemplars, anti-patterns
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS style_profiles (
  id            UUID PRIMARY KEY,
  subject_id    UUID NOT NULL REFERENCES subjects(id),
  profile_id    UUID NOT NULL REFERENCES personality_profiles(id) ON DELETE CASCADE,
  register      TEXT NOT NULL
    CHECK (register IN (
      'journal', 'casual_chat', 'technical', 'work_formal', 'social', 'notes'
    )),
  version       INT NOT NULL,
  features      JSONB NOT NULL DEFAULT '{}',
  n_texts       INT NOT NULL DEFAULT 0,
  n_words       INT NOT NULL DEFAULT 0,
  source_window JSONB NOT NULL DEFAULT '{}',
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (profile_id, register, version)
);
CREATE INDEX IF NOT EXISTS style_profiles_subject_register_idx
  ON style_profiles (subject_id, register, version DESC);

CREATE TABLE IF NOT EXISTS voice_exemplars (
  id                UUID PRIMARY KEY,
  subject_id        UUID NOT NULL REFERENCES subjects(id),
  authored_text_id  UUID REFERENCES authored_texts(id) ON DELETE CASCADE,
  context           JSONB NOT NULL DEFAULT '{}',
  response          TEXT NOT NULL,
  register          TEXT
    CHECK (register IS NULL OR register IN (
      'journal', 'casual_chat', 'technical', 'work_formal', 'social', 'notes'
    )),
  mode              TEXT,
  topic_tags        TEXT[] NOT NULL DEFAULT '{}',
  quality_score     REAL
    CHECK (quality_score IS NULL OR (quality_score >= 0 AND quality_score <= 1)),
  status            TEXT NOT NULL DEFAULT 'candidate'
    CHECK (status IN ('candidate', 'approved', 'rejected')),
  embedding         vector(768),
  feedback_turn_id  UUID REFERENCES chat_turns(id) ON DELETE SET NULL,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS voice_exemplars_subject_status_idx
  ON voice_exemplars (subject_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS voice_exemplars_authored_idx
  ON voice_exemplars (authored_text_id);

CREATE TABLE IF NOT EXISTS anti_patterns (
  id          UUID PRIMARY KEY,
  subject_id  UUID NOT NULL REFERENCES subjects(id),
  pattern     TEXT NOT NULL,
  kind        TEXT NOT NULL
    CHECK (kind IN ('phrase', 'regex', 'structure')),
  origin      TEXT NOT NULL
    CHECK (origin IN ('seed_slop_list', 'user_correction', 'eval_finding')),
  severity    SMALLINT NOT NULL DEFAULT 1
    CHECK (severity >= 1 AND severity <= 5),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (subject_id, pattern, kind)
);
CREATE INDEX IF NOT EXISTS anti_patterns_subject_idx
  ON anti_patterns (subject_id, severity DESC);

-- ---------------------------------------------------------------------------
-- Persona evaluation runs (batch harness output)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS persona_eval_runs (
  id                UUID PRIMARY KEY,
  subject_id        UUID NOT NULL REFERENCES subjects(id),
  suite_version     TEXT NOT NULL,
  profile_version   INT,
  composer_version  TEXT,
  model_id          TEXT,
  metrics           JSONB NOT NULL DEFAULT '{}',
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS persona_eval_runs_subject_idx
  ON persona_eval_runs (subject_id, created_at DESC);

CREATE TABLE IF NOT EXISTS persona_eval_results (
  id          UUID PRIMARY KEY,
  run_id      UUID NOT NULL REFERENCES persona_eval_runs(id) ON DELETE CASCADE,
  case_id     TEXT NOT NULL,
  scenario    TEXT,
  output      TEXT,
  metrics     JSONB NOT NULL DEFAULT '{}',
  judge_id    TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (run_id, case_id)
);
CREATE INDEX IF NOT EXISTS persona_eval_results_run_idx
  ON persona_eval_results (run_id);
