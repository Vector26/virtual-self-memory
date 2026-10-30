CREATE TABLE IF NOT EXISTS personality_profiles (
  id           UUID PRIMARY KEY,
  subject_id   UUID NOT NULL REFERENCES subjects(id),
  name         TEXT NOT NULL,
  version      INT NOT NULL DEFAULT 1,
  status       TEXT NOT NULL DEFAULT 'draft'
    CHECK (status IN ('draft', 'active', 'retired')),
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  activated_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS personality_one_active_per_subject
  ON personality_profiles (subject_id)
  WHERE status = 'active';

CREATE TABLE IF NOT EXISTS personality_facets (
  id                 UUID PRIMARY KEY,
  profile_id         UUID NOT NULL REFERENCES personality_profiles(id) ON DELETE CASCADE,
  facet_type         TEXT NOT NULL,
  mutability         TEXT NOT NULL DEFAULT 'approval_required'
    CHECK (mutability IN ('locked', 'approval_required', 'adaptive')),
  current_version_id UUID,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (profile_id, facet_type)
);

CREATE TABLE IF NOT EXISTS personality_facet_versions (
  id                   UUID PRIMARY KEY,
  facet_id             UUID NOT NULL REFERENCES personality_facets(id) ON DELETE CASCADE,
  version              INT NOT NULL,
  content              TEXT NOT NULL,
  priority             INT NOT NULL DEFAULT 0,
  confidence           REAL,
  status               TEXT NOT NULL DEFAULT 'active',
  approved_by          UUID REFERENCES actors(id),
  approved_at          TIMESTAMPTZ,
  supersedes_version_id UUID REFERENCES personality_facet_versions(id),
  created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (facet_id, version)
);

ALTER TABLE personality_facets
  DROP CONSTRAINT IF EXISTS personality_facets_current_version_fk;
ALTER TABLE personality_facets
  ADD CONSTRAINT personality_facets_current_version_fk
  FOREIGN KEY (current_version_id) REFERENCES personality_facet_versions(id);
