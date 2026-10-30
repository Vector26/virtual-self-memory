-- Virtual Self Memory — Phase 1 core schema
-- Apply: psql "$VSM_DATABASE_URL" -f migrations/001_initial.sql

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------------------
-- Identity
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS subjects (
  id          UUID PRIMARY KEY,
  display_name TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive')),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS actors (
  id                 UUID PRIMARY KEY,
  kind               TEXT NOT NULL CHECK (kind IN ('human', 'agent', 'service')),
  name               TEXT NOT NULL,
  credential_subject TEXT,
  status             TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'revoked')),
  created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS credentials (
  id              UUID PRIMARY KEY,
  actor_id        UUID NOT NULL REFERENCES actors(id),
  token_hash      BYTEA NOT NULL UNIQUE,
  scopes          TEXT[] NOT NULL DEFAULT '{}',
  issued_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  expires_at      TIMESTAMPTZ,
  last_used_at    TIMESTAMPTZ,
  rotated_from_id UUID REFERENCES credentials(id),
  revoked_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS credentials_actor_idx ON credentials (actor_id);

CREATE TABLE IF NOT EXISTS agent_registrations (
  id                      UUID PRIMARY KEY,
  actor_id                UUID NOT NULL REFERENCES actors(id),
  name                    TEXT NOT NULL,
  allowed_scopes          TEXT[] NOT NULL DEFAULT '{}',
  allowed_sensitivity_max SMALLINT NOT NULL DEFAULT 1,
  created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  revoked_at              TIMESTAMPTZ
);

-- ---------------------------------------------------------------------------
-- Artifact supertype (FK-backed lineage)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS artifacts (
  id            UUID PRIMARY KEY,
  artifact_type TEXT NOT NULL,
  subject_id    UUID NOT NULL REFERENCES subjects(id),
  sensitivity   SMALLINT NOT NULL DEFAULT 1,
  state         TEXT NOT NULL DEFAULT 'active',
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  deleted_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS artifacts_subject_idx ON artifacts (subject_id);

CREATE TABLE IF NOT EXISTS derivation_edges (
  id                 BIGSERIAL PRIMARY KEY,
  parent_artifact_id UUID NOT NULL REFERENCES artifacts(id),
  child_artifact_id  UUID NOT NULL REFERENCES artifacts(id),
  relationship       TEXT NOT NULL,
  created_by_job_id  UUID,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (parent_artifact_id, child_artifact_id, relationship)
);

-- ---------------------------------------------------------------------------
-- Evidence
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sources (
  id               UUID PRIMARY KEY,
  subject_id       UUID NOT NULL REFERENCES subjects(id),
  source_type      TEXT NOT NULL,
  external_id      TEXT,
  source_path      TEXT,
  source_uri       TEXT,
  content_hash     TEXT,
  event_time       TIMESTAMPTZ,
  observed_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  ingested_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  sensitivity      SMALLINT NOT NULL DEFAULT 1,
  retention_policy TEXT NOT NULL DEFAULT 'standard',
  encryption_key_id TEXT,
  deleted_at       TIMESTAMPTZ,
  UNIQUE (subject_id, source_type, external_id)
);

CREATE TABLE IF NOT EXISTS evidence_records (
  id                    UUID PRIMARY KEY REFERENCES artifacts(id),
  source_id             UUID NOT NULL REFERENCES sources(id),
  sequence              INT NOT NULL DEFAULT 0,
  content_ciphertext    BYTEA,
  content_plain         TEXT,
  structured_content    JSONB,
  speaker               TEXT,
  content_hash          TEXT NOT NULL,
  token_count           INT,
  sensitivity           SMALLINT NOT NULL DEFAULT 1,
  classification_status TEXT NOT NULL DEFAULT 'provisional'
    CHECK (classification_status IN ('provisional', 'classified', 'quarantined')),
  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  deleted_at            TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS evidence_source_idx ON evidence_records (source_id);

-- ---------------------------------------------------------------------------
-- Memories (versioned)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memories (
  id                 UUID PRIMARY KEY REFERENCES artifacts(id),
  subject_id         UUID NOT NULL REFERENCES subjects(id),
  kind               TEXT NOT NULL,
  scope              TEXT NOT NULL DEFAULT 'private',
  current_version_id UUID,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_retrieved_at  TIMESTAMPTZ,
  retrieval_count    INT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS memories_subject_idx ON memories (subject_id);

CREATE TABLE IF NOT EXISTS memory_versions (
  id                   UUID PRIMARY KEY REFERENCES artifacts(id),
  memory_id            UUID NOT NULL REFERENCES memories(id),
  version              INT NOT NULL,
  content              TEXT NOT NULL,
  structured_content   JSONB,
  status               TEXT NOT NULL DEFAULT 'active',
  importance           SMALLINT,
  confidence           REAL,
  trust                TEXT NOT NULL DEFAULT 'user_asserted',
  sensitivity          SMALLINT NOT NULL DEFAULT 1,
  valid_from           TIMESTAMPTZ,
  valid_to             TIMESTAMPTZ,
  event_time           TIMESTAMPTZ,
  change_reason        TEXT,
  created_by           UUID REFERENCES actors(id),
  created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  supersedes_version_id UUID REFERENCES memory_versions(id),
  UNIQUE (memory_id, version)
);
CREATE INDEX IF NOT EXISTS memory_versions_memory_idx ON memory_versions (memory_id);

ALTER TABLE memories
  DROP CONSTRAINT IF EXISTS memories_current_version_fk;
ALTER TABLE memories
  ADD CONSTRAINT memories_current_version_fk
  FOREIGN KEY (current_version_id) REFERENCES memory_versions(id);

CREATE TABLE IF NOT EXISTS memory_evidence (
  memory_id          UUID NOT NULL REFERENCES memories(id),
  memory_version_id  UUID NOT NULL REFERENCES memory_versions(id),
  evidence_record_id UUID NOT NULL REFERENCES evidence_records(id),
  relationship       TEXT NOT NULL,
  extractor          TEXT,
  extractor_version  TEXT,
  PRIMARY KEY (memory_version_id, evidence_record_id, relationship)
);

-- ---------------------------------------------------------------------------
-- Governance
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS idempotency_keys (
  key           TEXT PRIMARY KEY,
  actor_id      UUID NOT NULL REFERENCES actors(id),
  request_hash  TEXT NOT NULL,
  response_body JSONB NOT NULL,
  status_code   INT NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  expires_at    TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idempotency_expires_idx ON idempotency_keys (expires_at);

CREATE TABLE IF NOT EXISTS audit_events (
  id            BIGSERIAL PRIMARY KEY,
  request_id    TEXT,
  actor_id      UUID REFERENCES actors(id),
  operation     TEXT NOT NULL,
  target_type   TEXT,
  target_id     UUID,
  policy_decision TEXT,
  metadata      JSONB NOT NULL DEFAULT '{}',
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS audit_created_idx ON audit_events (created_at DESC);

CREATE TABLE IF NOT EXISTS memory_suppressions (
  id                UUID PRIMARY KEY,
  subject_id        UUID NOT NULL REFERENCES subjects(id),
  fingerprint_hmac  BYTEA NOT NULL,
  scope_source_id   UUID REFERENCES sources(id),
  reason            TEXT NOT NULL,
  policy            TEXT NOT NULL DEFAULT 'hard_delete',
  requested_by      UUID REFERENCES actors(id),
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  expires_at        TIMESTAMPTZ,
  UNIQUE (subject_id, fingerprint_hmac)
);

CREATE TABLE IF NOT EXISTS deletion_jobs (
  id              UUID PRIMARY KEY,
  actor_id        UUID NOT NULL REFERENCES actors(id),
  target_type     TEXT NOT NULL,
  target_id       UUID NOT NULL,
  mode            TEXT NOT NULL CHECK (mode IN ('memory', 'source', 'forget')),
  dependency_closure JSONB NOT NULL DEFAULT '[]',
  status          TEXT NOT NULL DEFAULT 'pending',
  preview_token   TEXT,
  requested_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  approved_at     TIMESTAMPTZ,
  completed_at    TIMESTAMPTZ,
  verification    JSONB
);

-- ---------------------------------------------------------------------------
-- Jobs / outbox
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jobs (
  id              UUID PRIMARY KEY,
  job_type        TEXT NOT NULL,
  dedupe_key      TEXT,
  payload_ref     JSONB NOT NULL DEFAULT '{}',
  state           TEXT NOT NULL DEFAULT 'pending'
    CHECK (state IN ('pending', 'running', 'completed', 'failed', 'dead')),
  attempts        INT NOT NULL DEFAULT 0,
  max_attempts    INT NOT NULL DEFAULT 5,
  available_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  lease_owner     TEXT,
  lease_expires   TIMESTAMPTZ,
  last_error      TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_dedupe_active_idx
  ON jobs (job_type, dedupe_key)
  WHERE dedupe_key IS NOT NULL AND state IN ('pending', 'running');

CREATE TABLE IF NOT EXISTS outbox_events (
  id           BIGSERIAL PRIMARY KEY,
  aggregate_id UUID NOT NULL,
  event_type   TEXT NOT NULL,
  payload      JSONB NOT NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  published_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS resource_leases (
  resource_name TEXT PRIMARY KEY,
  owner         TEXT NOT NULL,
  fencing_token BIGINT NOT NULL,
  acquired_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  renewed_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  expires_at    TIMESTAMPTZ NOT NULL
);
