-- Phase TR-2: per-chapter guided digests (GPG step 1+2), draft/proposed only

CREATE TABLE IF NOT EXISTS life_chapter_guided_digests (
  id                  UUID PRIMARY KEY,
  subject_id          UUID NOT NULL REFERENCES subjects(id),
  chapter_id          UUID NOT NULL REFERENCES life_chapters(id) ON DELETE CASCADE,
  status              TEXT NOT NULL DEFAULT 'draft'
    CHECK (status IN ('draft', 'proposed', 'active', 'rejected')),
  model_id            TEXT,
  prompt_version      TEXT NOT NULL,
  journaling_bias_note TEXT,
  metadata            JSONB NOT NULL DEFAULT '{}',
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS life_chapter_guided_digests_chapter_idx
  ON life_chapter_guided_digests (chapter_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS life_chapter_guided_digests_subject_idx
  ON life_chapter_guided_digests (subject_id, status);

CREATE TABLE IF NOT EXISTS life_chapter_guided_digest_items (
  id            UUID PRIMARY KEY,
  digest_id     UUID NOT NULL REFERENCES life_chapter_guided_digests(id) ON DELETE CASCADE,
  step          TEXT NOT NULL
    CHECK (step IN ('guided_qa', 'profile_sentence')),
  layer         TEXT NOT NULL
    CHECK (layer IN ('values', 'voice', 'decision_style', 'modes')),
  question_key  TEXT,
  text          TEXT NOT NULL,
  stance        TEXT
    CHECK (stance IS NULL OR stance IN ('actual', 'aspiration', 'mode_difference')),
  sort_order    INT NOT NULL DEFAULT 0,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS life_chapter_guided_digest_items_digest_idx
  ON life_chapter_guided_digest_items (digest_id, step, sort_order);

CREATE TABLE IF NOT EXISTS life_chapter_guided_digest_evidence (
  id          UUID PRIMARY KEY,
  item_id     UUID NOT NULL REFERENCES life_chapter_guided_digest_items(id) ON DELETE CASCADE,
  memory_id   UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  relationship TEXT NOT NULL DEFAULT 'supports'
    CHECK (relationship IN ('supports', 'contradicts', 'illustrates')),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (item_id, memory_id)
);

CREATE INDEX IF NOT EXISTS life_chapter_guided_digest_evidence_memory_idx
  ON life_chapter_guided_digest_evidence (memory_id);
