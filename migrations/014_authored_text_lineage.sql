-- E-3: lineage-aware deletion / suppression cascades (VSELF-66)

ALTER TABLE personality_facet_versions
  ADD COLUMN IF NOT EXISTS needs_review BOOLEAN NOT NULL DEFAULT false;

ALTER TABLE personality_proposals
  ADD COLUMN IF NOT EXISTS needs_review BOOLEAN NOT NULL DEFAULT false;

ALTER TABLE style_profiles
  ADD COLUMN IF NOT EXISTS recompute_pending BOOLEAN NOT NULL DEFAULT false;
