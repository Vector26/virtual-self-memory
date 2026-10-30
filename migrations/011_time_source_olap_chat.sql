-- T-3 follow-up: allow archive/chat resolver provenance labels on memories.time_source
-- and backfill the 308 rows the full backfill dated but could not label (CHECK rejected
-- 'olap_event' / 'chat_turn', so the runner left time_source NULL).

ALTER TABLE memories
  DROP CONSTRAINT IF EXISTS memories_time_source_check;

ALTER TABLE memories
  ADD CONSTRAINT memories_time_source_check
  CHECK (
    time_source IS NULL OR time_source = ANY (ARRAY[
      'explicit_in_text', 'relative_resolved', 'notion_created', 'notion_edited',
      'exif', 'email_header', 'calendar', 'inferred_from_chapter', 'import',
      'olap_event', 'chat_turn'
    ])
  );

-- Rows dated by the archive OLAP resolver: precision day, null source, archive scope.
UPDATE memories
SET time_source = 'olap_event'
WHERE time_source IS NULL AND time_precision = 'day' AND scope = 'archive';

-- Rows dated by the chat resolver: precision instant, null source, chat scope.
UPDATE memories
SET time_source = 'chat_turn'
WHERE time_source IS NULL AND time_precision = 'instant' AND scope = 'chat';
