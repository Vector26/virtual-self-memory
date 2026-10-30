-- Semantic + lexical retrieval projections (rebuildable from memory_versions)

ALTER TABLE memory_versions
  ADD COLUMN IF NOT EXISTS search_vector tsvector;

CREATE INDEX IF NOT EXISTS memory_versions_search_idx
  ON memory_versions USING GIN (search_vector);

CREATE TABLE IF NOT EXISTS memory_embeddings (
  memory_version_id UUID PRIMARY KEY REFERENCES memory_versions(id) ON DELETE CASCADE,
  model             TEXT NOT NULL,
  dims              INT NOT NULL,
  content_hash      TEXT NOT NULL,
  embedding         vector(768) NOT NULL,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS memory_embeddings_hnsw_idx
  ON memory_embeddings USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS index_state (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

INSERT INTO index_state (key, value) VALUES ('embed_model', 'nomic-embed-text')
ON CONFLICT (key) DO NOTHING;
