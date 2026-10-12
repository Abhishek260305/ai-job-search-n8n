-- 001: safety + scoring columns, deadline handling, Unicode-safe dedupe.
-- Applied once by postgres/migrate.sh (inside a transaction).

-- ---------------------------------------------------------------
-- Unicode-aware normalisers. The baseline used [^a-z0-9], which reduces a
-- non-Latin company or title to '' and could merge unrelated jobs.
-- ---------------------------------------------------------------
CREATE OR REPLACE FUNCTION norm_company(t text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
  SELECT regexp_replace(
           regexp_replace(lower(coalesce(t, '')),
             '\m(pvt|private|ltd|limited|inc|llc|llp|corp|corporation|co)\M\.?', '', 'g'),
           '[^[:alnum:]]+', '', 'g')
$$;

CREATE OR REPLACE FUNCTION norm_title(t text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
  SELECT btrim(regexp_replace(
           regexp_replace(lower(coalesce(t, '')), '\([^)]*\)', ' ', 'g'),
           '[^[:alnum:]]+', ' ', 'g'))
$$;

CREATE OR REPLACE FUNCTION norm_city(t text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
  SELECT regexp_replace(lower(split_part(coalesce(t, ''), ',', 1)), '[^[:alnum:]]+', '', 'g')
$$;

-- Guards: a name made only of symbols must not collapse to '' and match everything.
CREATE OR REPLACE FUNCTION safe_norm_company(t text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
  SELECT coalesce(nullif(norm_company(t), ''), 'c' || md5(coalesce(t, '')))
$$;

CREATE OR REPLACE FUNCTION safe_norm_title(t text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
  SELECT coalesce(nullif(norm_title(t), ''), 't' || md5(coalesce(t, '')))
$$;

CREATE OR REPLACE FUNCTION job_fingerprint(p_company text, p_title text, p_location text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
  SELECT md5(safe_norm_company(p_company) || '|' || safe_norm_title(p_title) || '|' || norm_city(p_location))
$$;

-- ---------------------------------------------------------------
-- New job columns
-- ---------------------------------------------------------------
ALTER TABLE jobs
  ADD COLUMN scam_risk        text CHECK (scam_risk IN ('low','medium','high')),
  ADD COLUMN scam_score       int,
  ADD COLUMN scam_flags       jsonb NOT NULL DEFAULT '[]',
  ADD COLUMN deadline         date,
  ADD COLUMN posting_language text,
  ADD COLUMN yoe_min          int,
  ADD COLUMN yoe_max          int,
  ADD COLUMN scores           jsonb,   -- {technical, experience, behavioral, career}, each 0-100
  ADD COLUMN gates            jsonb,   -- {eligibility, language, location}: {verdict, note}
  ADD COLUMN verdict          text CHECK (verdict IN ('strong','good','moderate','weak','poor','excluded')),
  ADD COLUMN strengths        jsonb NOT NULL DEFAULT '[]',
  ADD COLUMN gaps             jsonb NOT NULL DEFAULT '[]';

ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_status_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_status_check CHECK (status IN (
  'new','filtered_out','scam_blocked','scored','shortlisted','tailored',
  'approved','applied','skipped','expired'));

CREATE INDEX jobs_deadline_idx ON jobs (deadline) WHERE deadline IS NOT NULL;
CREATE INDEX jobs_scam_risk_idx ON jobs (scam_risk);

-- Re-derive normalised columns for any existing rows with the new functions.
UPDATE jobs SET
  norm_title   = safe_norm_title(title),
  norm_company = safe_norm_company(company),
  norm_city    = norm_city(location),
  fingerprint  = job_fingerprint(company, title, location);

-- ---------------------------------------------------------------
-- upsert_job v2: same dedupe layers, plus safety/deadline/language fields.
-- New parameters are trailing and optional, so existing callers keep working.
-- ---------------------------------------------------------------
DROP FUNCTION IF EXISTS upsert_job(text, text, text, text, text, text, text, boolean,
                                   timestamptz, text, numeric, numeric, text, jsonb);

CREATE FUNCTION upsert_job(
  p_source text, p_source_job_id text, p_url text, p_apply_url text,
  p_title text, p_company text, p_location text, p_is_remote boolean,
  p_posted_at timestamptz, p_description text,
  p_salary_min numeric, p_salary_max numeric, p_salary_currency text,
  p_raw jsonb,
  p_deadline date DEFAULT NULL,
  p_scam_risk text DEFAULT NULL,
  p_scam_score int DEFAULT NULL,
  p_scam_flags jsonb DEFAULT NULL,
  p_posting_language text DEFAULT NULL
) RETURNS TABLE (job_id bigint, is_new boolean)
LANGUAGE plpgsql AS $$
DECLARE
  v_job_id bigint;
  v_nt text := safe_norm_title(p_title);
  v_nc text := safe_norm_company(p_company);
  v_ncity text := norm_city(p_location);
  v_fp text := job_fingerprint(p_company, p_title, p_location);
  v_dh text := CASE WHEN p_description IS NULL OR p_description = '' THEN NULL
                    ELSE md5(lower(regexp_replace(p_description, '\s+', ' ', 'g'))) END;
  v_new boolean := false;
BEGIN
  -- 1. exact: same source listing seen before
  SELECT js.job_id INTO v_job_id
    FROM job_sources js WHERE js.source = p_source AND js.source_job_id = p_source_job_id;

  -- 2. fingerprint
  IF v_job_id IS NULL THEN
    SELECT j.id INTO v_job_id FROM jobs j WHERE j.fingerprint = v_fp;
  END IF;

  -- 3. fuzzy title, same company + city
  IF v_job_id IS NULL THEN
    SELECT j.id INTO v_job_id FROM jobs j
     WHERE j.norm_company = v_nc AND j.norm_city = v_ncity
       AND similarity(j.norm_title, v_nt) > 0.85
     ORDER BY similarity(j.norm_title, v_nt) DESC LIMIT 1;
  END IF;

  -- 4. identical description at same company
  IF v_job_id IS NULL AND v_dh IS NOT NULL THEN
    SELECT j.id INTO v_job_id FROM jobs j
     WHERE j.norm_company = v_nc AND j.desc_hash = v_dh LIMIT 1;
  END IF;

  IF v_job_id IS NULL THEN
    INSERT INTO jobs (fingerprint, title, company, location, is_remote, posted_at,
                      description, salary_min, salary_max, salary_currency, canonical_url,
                      norm_title, norm_company, norm_city, desc_hash,
                      deadline, scam_risk, scam_score, scam_flags, posting_language,
                      status, filter_reason)
    VALUES (v_fp, p_title, p_company, p_location, p_is_remote, p_posted_at,
            p_description, p_salary_min, p_salary_max, p_salary_currency,
            coalesce(p_apply_url, p_url), v_nt, v_nc, v_ncity, v_dh,
            p_deadline, p_scam_risk, p_scam_score, coalesce(p_scam_flags, '[]'::jsonb), p_posting_language,
            CASE WHEN p_scam_risk = 'high' THEN 'scam_blocked' ELSE 'new' END,
            CASE WHEN p_scam_risk = 'high' THEN 'scam risk: high' ELSE NULL END)
    RETURNING id INTO v_job_id;
    v_new := true;
  ELSE
    UPDATE jobs SET
      last_seen   = now(),
      status      = CASE
                      WHEN p_scam_risk = 'high' AND status IN ('new','scored','shortlisted') THEN 'scam_blocked'
                      WHEN status = 'expired' THEN 'new'
                      ELSE status END,
      filter_reason = CASE WHEN p_scam_risk = 'high' AND status IN ('new','scored','shortlisted')
                           THEN 'scam risk: high' ELSE filter_reason END,
      description = CASE WHEN coalesce(length(description),0) < coalesce(length(p_description),0)
                         THEN p_description ELSE description END,
      canonical_url = CASE WHEN p_apply_url IS NOT NULL THEN p_apply_url ELSE canonical_url END,
      salary_min  = coalesce(salary_min, p_salary_min),
      salary_max  = coalesce(salary_max, p_salary_max),
      salary_currency = coalesce(salary_currency, p_salary_currency),
      deadline    = coalesce(p_deadline, deadline),
      posting_language = coalesce(p_posting_language, posting_language),
      scam_risk   = coalesce(p_scam_risk, scam_risk),
      scam_score  = coalesce(p_scam_score, scam_score),
      scam_flags  = coalesce(p_scam_flags, scam_flags)
    WHERE id = v_job_id;
  END IF;

  INSERT INTO job_sources (job_id, source, source_job_id, url, apply_url, raw_json)
  VALUES (v_job_id, p_source, p_source_job_id, p_url, p_apply_url, p_raw)
  ON CONFLICT (source, source_job_id)
  DO UPDATE SET last_seen = now(), url = EXCLUDED.url,
                apply_url = coalesce(EXCLUDED.apply_url, job_sources.apply_url),
                raw_json = EXCLUDED.raw_json;

  RETURN QUERY SELECT v_job_id, v_new;
END;
$$;

-- ---------------------------------------------------------------
-- Deadline sweep and shortlist view
-- ---------------------------------------------------------------
-- Retire open jobs whose stated deadline has passed. Jobs with no deadline are never touched.
CREATE OR REPLACE FUNCTION sweep_expired() RETURNS int
LANGUAGE sql AS $$
  WITH u AS (
    UPDATE jobs SET status = 'expired'
     WHERE deadline < current_date
       AND status IN ('new','scored','shortlisted','tailored')
    RETURNING 1)
  SELECT count(*)::int FROM u
$$;

CREATE OR REPLACE VIEW v_shortlist AS
SELECT j.id, j.title, j.company, j.location, j.score, j.verdict, j.scam_risk,
       j.deadline,
       (j.deadline IS NOT NULL AND j.deadline BETWEEN current_date AND current_date + 7) AS closing_soon,
       (j.posted_at IS NOT NULL AND j.posted_at < now() - interval '30 days') AS stale,
       j.canonical_url, j.strengths, j.gaps, j.gates, j.first_seen
  FROM jobs j
 WHERE j.status IN ('scored','shortlisted')
   AND coalesce(j.verdict, '') <> 'excluded';
