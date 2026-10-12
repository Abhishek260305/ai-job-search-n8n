-- 003: pipeline plumbing: collection rotation, scoring claims, digest tracking.

ALTER TABLE jobs
  ADD COLUMN scoring_claimed_at timestamptz,
  ADD COLUMN score_attempts     int NOT NULL DEFAULT 0,
  ADD COLUMN notified_at        timestamptz;

CREATE INDEX jobs_to_score_idx ON jobs (status, scoring_claimed_at) WHERE status = 'new';

-- Rotating cursor through the (term x location x source) combinations, so each collection
-- run takes the next slice instead of scraping everything at once.
CREATE TABLE collect_cursor (
  profile    text PRIMARY KEY,
  position   int NOT NULL DEFAULT 0,
  updated_at timestamptz NOT NULL DEFAULT now()
);

-- Returns the start position for this run and advances the cursor by p_batch (wrapping).
CREATE OR REPLACE FUNCTION next_collect_slice(p_profile text, p_total int, p_batch int) RETURNS int
LANGUAGE plpgsql AS $$
DECLARE v_start int;
BEGIN
  IF p_total <= 0 THEN RETURN 0; END IF;
  INSERT INTO collect_cursor (profile, position) VALUES (p_profile, 0)
    ON CONFLICT (profile) DO NOTHING;
  SELECT position % p_total INTO v_start FROM collect_cursor WHERE profile = p_profile FOR UPDATE;
  UPDATE collect_cursor SET position = (v_start + p_batch) % p_total, updated_at = now()
   WHERE profile = p_profile;
  RETURN v_start;
END;
$$;

-- Claim up to p_limit unscored jobs for a scoring run. A claim is a lease: if the run dies,
-- the job becomes claimable again after p_lease. Jobs that failed validation p_max_attempts
-- times are left alone for manual review. Newest postings first.
CREATE OR REPLACE FUNCTION claim_jobs_to_score(
  p_limit int, p_max_attempts int DEFAULT 3, p_lease interval DEFAULT interval '30 minutes'
) RETURNS TABLE (id bigint, title text, company text, location text, posted_at timestamptz, description text)
LANGUAGE sql AS $$
  UPDATE jobs j SET scoring_claimed_at = now()
   WHERE j.id IN (
     SELECT c.id FROM jobs c
      WHERE c.status = 'new'
        AND coalesce(c.scam_risk, 'low') <> 'high'
        AND c.score_attempts < p_max_attempts
        AND (c.scoring_claimed_at IS NULL OR c.scoring_claimed_at < now() - p_lease)
      ORDER BY c.posted_at DESC NULLS LAST, c.id DESC
      LIMIT p_limit
      FOR UPDATE SKIP LOCKED)
  RETURNING j.id, j.title, j.company, j.location, j.posted_at, j.description
$$;

-- An invalid model reply: count it and release the claim.
CREATE OR REPLACE FUNCTION note_score_failure(p_job_id bigint) RETURNS int
LANGUAGE sql AS $$
  UPDATE jobs SET score_attempts = score_attempts + 1, scoring_claimed_at = NULL
   WHERE id = p_job_id RETURNING score_attempts
$$;

-- record_score from 002, now also releasing the scoring claim.
CREATE OR REPLACE FUNCTION record_score(p_job_id bigint, p jsonb) RETURNS text
LANGUAGE plpgsql AS $$
DECLARE
  v_status text;
BEGIN
  UPDATE jobs SET
    score         = (p->>'score')::int,
    verdict       = p->>'verdict',
    scores        = p->'scores',
    gates         = p->'gates',
    strengths     = coalesce(p->'strengths', '[]'::jsonb),
    gaps          = coalesce(p->'gaps', '[]'::jsonb),
    score_reason  = p->>'score_reason',
    scored_at     = now(),
    scoring_claimed_at = NULL,
    deadline      = coalesce(nullif(p->>'deadline', '')::date, deadline),
    posting_language = coalesce(p->>'posting_language', posting_language),
    yoe_min       = coalesce((p->>'yoe_min')::int, yoe_min),
    yoe_max       = coalesce((p->>'yoe_max')::int, yoe_max),
    status        = CASE WHEN status IN ('new', 'scored', 'shortlisted', 'filtered_out')
                         THEN p->>'next_status' ELSE status END,
    filter_reason = CASE WHEN status IN ('new', 'scored', 'shortlisted', 'filtered_out')
                         THEN p->>'filter_reason' ELSE filter_reason END
  WHERE id = p_job_id
  RETURNING status INTO v_status;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'record_score: job % not found', p_job_id;
  END IF;
  RETURN v_status;
END;
$$;
