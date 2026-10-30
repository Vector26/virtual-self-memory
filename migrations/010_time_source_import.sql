-- T-2: flag bulk Notion import timestamps without treating them as authored dates.

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

ALTER TABLE authored_texts DROP CONSTRAINT IF EXISTS authored_texts_time_source_check;
ALTER TABLE authored_texts
  ADD CONSTRAINT authored_texts_time_source_check
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
