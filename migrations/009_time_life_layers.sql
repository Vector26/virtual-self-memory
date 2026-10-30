-- Phase T (part 1): layered time model — strands, chapters, patterns, temporal columns
-- See docs/PERSONA_PIPELINE_PLAN.md §15.2 and §15.4

-- Shared CHECK labels (match authored_texts from 008_persona_a.sql)
-- time_precision: instant | day | month | year | chapter | unknown
-- time_source: explicit_in_text | relative_resolved | notion_created | notion_edited
--              exif | email_header | calendar | inferred_from_chapter

-- ---------------------------------------------------------------------------
-- Life layers (T1–T3)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS life_strands (
  id           UUID PRIMARY KEY,
  slug         TEXT NOT NULL UNIQUE,
  display_name TEXT NOT NULL,
  sort_order   INT NOT NULL DEFAULT 0,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS life_chapters (
  id                   UUID PRIMARY KEY,
  subject_id           UUID NOT NULL REFERENCES subjects(id),
  strand_id            UUID NOT NULL REFERENCES life_strands(id),
  title                TEXT NOT NULL,
  summary              TEXT,
  status               TEXT NOT NULL DEFAULT 'proposed'
    CHECK (status IN ('proposed', 'active', 'closed', 'rejected')),
  valid_from           TIMESTAMPTZ,
  valid_to             TIMESTAMPTZ,
  time_precision       TEXT NOT NULL DEFAULT 'unknown'
    CHECK (time_precision IN (
      'instant', 'day', 'month', 'year', 'chapter', 'unknown'
    )),
  valid_from_precision TEXT NOT NULL DEFAULT 'unknown'
    CHECK (valid_from_precision IN (
      'instant', 'day', 'month', 'year', 'chapter', 'unknown'
    )),
  valid_to_precision   TEXT NOT NULL DEFAULT 'unknown'
    CHECK (valid_to_precision IN (
      'instant', 'day', 'month', 'year', 'chapter', 'unknown'
    )),
  edge_fuzz_days       INT NOT NULL DEFAULT 0
    CHECK (edge_fuzz_days >= 0),
  created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  approved_at          TIMESTAMPTZ,
  approved_by          UUID REFERENCES actors(id)
);

CREATE INDEX IF NOT EXISTS life_chapters_subject_strand_idx
  ON life_chapters (subject_id, strand_id, status);
CREATE INDEX IF NOT EXISTS life_chapters_validity_idx
  ON life_chapters (subject_id, valid_from, valid_to);

CREATE TABLE IF NOT EXISTS life_patterns (
  id             UUID PRIMARY KEY,
  subject_id     UUID NOT NULL REFERENCES subjects(id),
  chapter_id     UUID NOT NULL REFERENCES life_chapters(id) ON DELETE CASCADE,
  name           TEXT NOT NULL,
  description    TEXT,
  status         TEXT NOT NULL DEFAULT 'proposed'
    CHECK (status IN ('proposed', 'active', 'paused', 'ended')),
  valid_from     TIMESTAMPTZ,
  valid_to       TIMESTAMPTZ,
  time_precision TEXT NOT NULL DEFAULT 'unknown'
    CHECK (time_precision IN (
      'instant', 'day', 'month', 'year', 'chapter', 'unknown'
    )),
  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS life_patterns_chapter_idx
  ON life_patterns (chapter_id, status);
CREATE INDEX IF NOT EXISTS life_patterns_subject_idx
  ON life_patterns (subject_id, valid_from, valid_to);

CREATE TABLE IF NOT EXISTS chapter_links (
  id              UUID PRIMARY KEY,
  subject_id      UUID NOT NULL REFERENCES subjects(id),
  from_chapter_id UUID NOT NULL REFERENCES life_chapters(id) ON DELETE CASCADE,
  to_chapter_id   UUID NOT NULL REFERENCES life_chapters(id) ON DELETE CASCADE,
  relationship    TEXT NOT NULL
    CHECK (relationship IN ('overlaps', 'precedes', 'succeeds', 'parallel', 'related')),
  notes           TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CHECK (from_chapter_id <> to_chapter_id),
  UNIQUE (from_chapter_id, to_chapter_id, relationship)
);

CREATE INDEX IF NOT EXISTS chapter_links_subject_idx
  ON chapter_links (subject_id);

-- Seed canonical strands (global catalog)
INSERT INTO life_strands (id, slug, display_name, sort_order)
VALUES
  ('a1000001-0001-4001-8001-000000000001', 'career', 'Career', 1),
  ('a1000001-0001-4001-8001-000000000002', 'relationships', 'Relationships', 2),
  ('a1000001-0001-4001-8001-000000000003', 'mind', 'Mind & health', 3),
  ('a1000001-0001-4001-8001-000000000004', 'craft', 'Craft & hobbies', 4),
  ('a1000001-0001-4001-8001-000000000005', 'place', 'Place', 5)
ON CONFLICT (slug) DO NOTHING;

-- ---------------------------------------------------------------------------
-- Temporal columns on memories (episode head)
-- ---------------------------------------------------------------------------
ALTER TABLE memories ADD COLUMN IF NOT EXISTS authored_at TIMESTAMPTZ;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS valid_from TIMESTAMPTZ;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS valid_to TIMESTAMPTZ;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS time_precision TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE memories ADD COLUMN IF NOT EXISTS time_source TEXT;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE memories ADD COLUMN IF NOT EXISTS invalidated_at TIMESTAMPTZ;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS chapter_id UUID REFERENCES life_chapters(id);
ALTER TABLE memories ADD COLUMN IF NOT EXISTS pattern_id UUID REFERENCES life_patterns(id);

ALTER TABLE memories DROP CONSTRAINT IF EXISTS memories_time_precision_check;
ALTER TABLE memories
  ADD CONSTRAINT memories_time_precision_check
  CHECK (time_precision IN (
    'instant', 'day', 'month', 'year', 'chapter', 'unknown'
  ));

ALTER TABLE memories DROP CONSTRAINT IF EXISTS memories_time_source_check;
ALTER TABLE memories
  ADD CONSTRAINT memories_time_source_check
  CHECK (time_source IS NULL OR time_source IN (
    'explicit_in_text',
    'relative_resolved',
    'notion_created',
    'notion_edited',
    'exif',
    'email_header',
    'calendar',
    'inferred_from_chapter',
    'import',
    'olap_event',
    'chat_turn'
  ));

CREATE INDEX IF NOT EXISTS memories_chapter_idx
  ON memories (subject_id, chapter_id) WHERE chapter_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS memories_validity_idx
  ON memories (subject_id, valid_from, valid_to);

-- memory_versions: align with §15.4 (versions keep their own validity window)
ALTER TABLE memory_versions ADD COLUMN IF NOT EXISTS authored_at TIMESTAMPTZ;
ALTER TABLE memory_versions ADD COLUMN IF NOT EXISTS time_precision TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE memory_versions ADD COLUMN IF NOT EXISTS time_source TEXT;
ALTER TABLE memory_versions ADD COLUMN IF NOT EXISTS ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE memory_versions ADD COLUMN IF NOT EXISTS invalidated_at TIMESTAMPTZ;
ALTER TABLE memory_versions ADD COLUMN IF NOT EXISTS chapter_id UUID REFERENCES life_chapters(id);
ALTER TABLE memory_versions ADD COLUMN IF NOT EXISTS pattern_id UUID REFERENCES life_patterns(id);

ALTER TABLE memory_versions DROP CONSTRAINT IF EXISTS memory_versions_time_precision_check;
ALTER TABLE memory_versions
  ADD CONSTRAINT memory_versions_time_precision_check
  CHECK (time_precision IN (
    'instant', 'day', 'month', 'year', 'chapter', 'unknown'
  ));

ALTER TABLE memory_versions DROP CONSTRAINT IF EXISTS memory_versions_time_source_check;
ALTER TABLE memory_versions
  ADD CONSTRAINT memory_versions_time_source_check
  CHECK (time_source IS NULL OR time_source IN (
    'explicit_in_text',
    'relative_resolved',
    'notion_created',
    'notion_edited',
    'exif',
    'email_header',
    'calendar',
    'inferred_from_chapter',
    'import',
    'olap_event',
    'chat_turn'
  ));

-- authored_texts: time columns from 008_persona_a; add chapter/pattern linkage
ALTER TABLE authored_texts ADD COLUMN IF NOT EXISTS chapter_id UUID REFERENCES life_chapters(id);
ALTER TABLE authored_texts ADD COLUMN IF NOT EXISTS pattern_id UUID REFERENCES life_patterns(id);

CREATE INDEX IF NOT EXISTS authored_texts_chapter_idx
  ON authored_texts (subject_id, chapter_id) WHERE chapter_id IS NOT NULL;

-- Archive / OLAP-facing rows (sources + evidence)
ALTER TABLE sources ADD COLUMN IF NOT EXISTS authored_at TIMESTAMPTZ;
ALTER TABLE sources ADD COLUMN IF NOT EXISTS valid_from TIMESTAMPTZ;
ALTER TABLE sources ADD COLUMN IF NOT EXISTS valid_to TIMESTAMPTZ;
ALTER TABLE sources ADD COLUMN IF NOT EXISTS time_precision TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE sources ADD COLUMN IF NOT EXISTS time_source TEXT;
ALTER TABLE sources ADD COLUMN IF NOT EXISTS invalidated_at TIMESTAMPTZ;

ALTER TABLE sources DROP CONSTRAINT IF EXISTS sources_time_precision_check;
ALTER TABLE sources
  ADD CONSTRAINT sources_time_precision_check
  CHECK (time_precision IN (
    'instant', 'day', 'month', 'year', 'chapter', 'unknown'
  ));

ALTER TABLE sources DROP CONSTRAINT IF EXISTS sources_time_source_check;
ALTER TABLE sources
  ADD CONSTRAINT sources_time_source_check
  CHECK (time_source IS NULL OR time_source IN (
    'explicit_in_text',
    'relative_resolved',
    'notion_created',
    'notion_edited',
    'exif',
    'email_header',
    'calendar',
    'inferred_from_chapter',
    'import',
    'olap_event',
    'chat_turn'
  ));

ALTER TABLE evidence_records ADD COLUMN IF NOT EXISTS authored_at TIMESTAMPTZ;
ALTER TABLE evidence_records ADD COLUMN IF NOT EXISTS valid_from TIMESTAMPTZ;
ALTER TABLE evidence_records ADD COLUMN IF NOT EXISTS valid_to TIMESTAMPTZ;
ALTER TABLE evidence_records ADD COLUMN IF NOT EXISTS time_precision TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE evidence_records ADD COLUMN IF NOT EXISTS time_source TEXT;
ALTER TABLE evidence_records ADD COLUMN IF NOT EXISTS ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE evidence_records ADD COLUMN IF NOT EXISTS invalidated_at TIMESTAMPTZ;
ALTER TABLE evidence_records ADD COLUMN IF NOT EXISTS chapter_id UUID REFERENCES life_chapters(id);
ALTER TABLE evidence_records ADD COLUMN IF NOT EXISTS pattern_id UUID REFERENCES life_patterns(id);

ALTER TABLE evidence_records DROP CONSTRAINT IF EXISTS evidence_records_time_precision_check;
ALTER TABLE evidence_records
  ADD CONSTRAINT evidence_records_time_precision_check
  CHECK (time_precision IN (
    'instant', 'day', 'month', 'year', 'chapter', 'unknown'
  ));

ALTER TABLE evidence_records DROP CONSTRAINT IF EXISTS evidence_records_time_source_check;
ALTER TABLE evidence_records
  ADD CONSTRAINT evidence_records_time_source_check
  CHECK (time_source IS NULL OR time_source IN (
    'explicit_in_text',
    'relative_resolved',
    'notion_created',
    'notion_edited',
    'exif',
    'email_header',
    'calendar',
    'inferred_from_chapter',
    'import',
    'olap_event',
    'chat_turn'
  ));

-- Persona snapshots attach to chapters (§15.6)
ALTER TABLE persona_modes ADD COLUMN IF NOT EXISTS chapter_id UUID REFERENCES life_chapters(id);

ALTER TABLE personality_facet_versions ADD COLUMN IF NOT EXISTS authored_at TIMESTAMPTZ;
ALTER TABLE personality_facet_versions ADD COLUMN IF NOT EXISTS valid_from TIMESTAMPTZ;
ALTER TABLE personality_facet_versions ADD COLUMN IF NOT EXISTS valid_to TIMESTAMPTZ;
ALTER TABLE personality_facet_versions ADD COLUMN IF NOT EXISTS time_precision TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE personality_facet_versions ADD COLUMN IF NOT EXISTS time_source TEXT;
ALTER TABLE personality_facet_versions ADD COLUMN IF NOT EXISTS ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE personality_facet_versions ADD COLUMN IF NOT EXISTS invalidated_at TIMESTAMPTZ;
ALTER TABLE personality_facet_versions ADD COLUMN IF NOT EXISTS chapter_id UUID REFERENCES life_chapters(id);

ALTER TABLE personality_facet_versions DROP CONSTRAINT IF EXISTS personality_facet_versions_time_precision_check;
ALTER TABLE personality_facet_versions
  ADD CONSTRAINT personality_facet_versions_time_precision_check
  CHECK (time_precision IN (
    'instant', 'day', 'month', 'year', 'chapter', 'unknown'
  ));

ALTER TABLE personality_facet_versions DROP CONSTRAINT IF EXISTS personality_facet_versions_time_source_check;
ALTER TABLE personality_facet_versions
  ADD CONSTRAINT personality_facet_versions_time_source_check
  CHECK (time_source IS NULL OR time_source IN (
    'explicit_in_text',
    'relative_resolved',
    'notion_created',
    'notion_edited',
    'exif',
    'email_header',
    'calendar',
    'inferred_from_chapter',
    'import',
    'olap_event',
    'chat_turn'
  ));
