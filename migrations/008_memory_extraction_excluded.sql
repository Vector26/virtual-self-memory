-- Allow audit rows when LLM proposed assistant-only memory candidates (self-contamination guard).

ALTER TABLE memory_extraction_runs
  DROP CONSTRAINT IF EXISTS memory_extraction_runs_decision_check;

ALTER TABLE memory_extraction_runs
  ADD CONSTRAINT memory_extraction_runs_decision_check
  CHECK (decision IN ('saved', 'skipped', 'failed', 'excluded'));
