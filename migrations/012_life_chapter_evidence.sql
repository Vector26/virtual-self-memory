-- Phase T (part 4): evidence lineage for proposed life chapters (T-4)

CREATE TABLE IF NOT EXISTS life_chapter_evidence (
  id                  UUID PRIMARY KEY,
  chapter_id          UUID NOT NULL REFERENCES life_chapters(id) ON DELETE CASCADE,
  memory_id           UUID REFERENCES memories(id) ON DELETE CASCADE,
  evidence_record_id  UUID REFERENCES evidence_records(id) ON DELETE CASCADE,
  relationship        TEXT NOT NULL DEFAULT 'supporting'
    CHECK (relationship IN ('supporting', 'boundary', 'theme')),
  note                TEXT,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CHECK (memory_id IS NOT NULL OR evidence_record_id IS NOT NULL),
  UNIQUE (chapter_id, memory_id),
  UNIQUE (chapter_id, evidence_record_id)
);

CREATE INDEX IF NOT EXISTS life_chapter_evidence_chapter_idx
  ON life_chapter_evidence (chapter_id);
CREATE INDEX IF NOT EXISTS life_chapter_evidence_memory_idx
  ON life_chapter_evidence (memory_id) WHERE memory_id IS NOT NULL;
