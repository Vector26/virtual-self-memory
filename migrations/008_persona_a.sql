-- Persona pipeline phase 1 (part A): authorship corpus + chat feedback
-- See docs/PERSONA_PIPELINE_PLAN.md §4 and §15.4

CREATE TABLE IF NOT EXISTS authored_texts (
  id                UUID PRIMARY KEY,
  subject_id        UUID NOT NULL REFERENCES subjects(id),
  source_kind       TEXT NOT NULL
    CHECK (source_kind IN (
      'notion', 'chat_user_turn', 'gmail_sent', 'keep', 'meta_message', 'manual'
    )),
  source_ref        JSONB NOT NULL DEFAULT '{}',
  author            TEXT NOT NULL
    CHECK (author IN (
      'self', 'llm', 'other_person', 'quoted', 'template', 'unknown'
    )),
  author_confidence REAL
    CHECK (
      author_confidence IS NULL
      OR (author_confidence >= 0 AND author_confidence <= 1)
    ),
  register          TEXT NOT NULL
    CHECK (register IN (
      'journal', 'casual_chat', 'technical', 'work_formal', 'social', 'notes'
    )),
  language          TEXT,
  content           TEXT NOT NULL,
  word_count        INT NOT NULL DEFAULT 0,
  text_hash         TEXT NOT NULL,
  sensitivity       SMALLINT NOT NULL DEFAULT 1,
  authored_at       TIMESTAMPTZ,
  valid_from        TIMESTAMPTZ,
  valid_to          TIMESTAMPTZ,
  time_precision    TEXT NOT NULL DEFAULT 'unknown'
    CHECK (time_precision IN (
      'instant', 'day', 'month', 'year', 'chapter', 'unknown'
    )),
  time_source       TEXT
    CHECK (time_source IS NULL OR time_source IN (
      'explicit_in_text',
      'relative_resolved',
      'notion_created',
      'notion_edited',
      'exif',
      'email_header',
      'calendar',
      'inferred_from_chapter'
    )),
  ingested_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  invalidated_at    TIMESTAMPTZ,
  mode_hint         TEXT,
  split             TEXT NOT NULL DEFAULT 'train'
    CHECK (split IN ('train', 'heldout')),
  excluded_reason   TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (subject_id, text_hash)
);

CREATE INDEX IF NOT EXISTS authored_texts_subject_idx
  ON authored_texts (subject_id, created_at DESC);
CREATE INDEX IF NOT EXISTS authored_texts_author_idx
  ON authored_texts (subject_id, author);
CREATE INDEX IF NOT EXISTS authored_texts_register_idx
  ON authored_texts (subject_id, register);
CREATE INDEX IF NOT EXISTS authored_texts_split_idx
  ON authored_texts (subject_id, split);

CREATE TABLE IF NOT EXISTS chat_turn_feedback (
  id         UUID PRIMARY KEY,
  turn_id    UUID NOT NULL REFERENCES chat_turns(id) ON DELETE CASCADE,
  rating     TEXT NOT NULL
    CHECK (rating IN ('sounds_like_me', 'off', 'wrong_fact')),
  note       TEXT,
  rewrite    TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (turn_id)
);

CREATE INDEX IF NOT EXISTS chat_turn_feedback_turn_idx
  ON chat_turn_feedback (turn_id);
