-- 004: better cross-source dedupe for messy locations (found on real data).
--  * Indeed returns state codes ("MH, India"), LinkedIn "Pune Division, Maharashtra, India",
--    Naukri "Pune, Hyderabad, Bengaluru, India": the same job got a different city key per board.
--  * A coarse location (state code, country only, "Remote") may now match a job in any city.
--  * Two different named cities match only when the descriptions are nearly identical
--    (so the same title in two real offices is still kept apart).

-- City key: drop generic words so "Pune Division/City/District" and "Greater Bengaluru Area" collapse.
CREATE OR REPLACE FUNCTION norm_city(t text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
  SELECT regexp_replace(
           regexp_replace(
             regexp_replace(lower(split_part(coalesce(t, ''), ',', 1)), '^\s*greater\s+', ''),
             '\s+(division|district|city|metropolitan area|metropolitan region|region|area|urban|east|west|north|south|central)\s*$',
             ''),
           '[^[:alnum:]]+', '', 'g')
$$;

-- A city key too coarse to tell two offices apart: state/country codes and "remote".
CREATE OR REPLACE FUNCTION is_coarse_city(c text) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
  SELECT c IS NULL OR c = '' OR length(c) <= 3
      OR c IN ('india', 'remote', 'anywhere', 'worldwide', 'unitedstates', 'unitedkingdom', 'usa', 'uk')
$$;

-- Normalised start of a description, for comparing the same posting across boards.
CREATE OR REPLACE FUNCTION desc_key(t text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
  SELECT left(regexp_replace(lower(coalesce(t, '')), '[^[:alnum:]]+', ' ', 'g'), 1500)
$$;

-- Same-posting test shared by upsert_job and merge_duplicate_jobs.
CREATE OR REPLACE FUNCTION same_posting(
  c1 text, t1 text, city1 text, d1 text, c2 text, t2 text, city2 text, d2 text
) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
  SELECT c1 = c2
     AND similarity(t1, t2) > 0.85
     AND (city1 = city2 OR is_coarse_city(city1) OR is_coarse_city(city2)
          OR similarity(desc_key(d1), desc_key(d2)) > 0.8)
$$;

-- Re-derive the city key for existing rows (fingerprints are recomputed after merging below).
UPDATE jobs SET norm_city = norm_city(location);

-- Merge duplicates already stored, keeping the oldest row. Jobs in motion (tailored, approved,
-- applied) are never merged or deleted.
CREATE OR REPLACE FUNCTION merge_duplicate_jobs() RETURNS int
LANGUAGE plpgsql AS $$
DECLARE r record; n int := 0;
BEGIN
  FOR r IN
    SELECT a.id AS keep_id, b.id AS drop_id
      FROM jobs a JOIN jobs b ON a.id < b.id
     WHERE a.status NOT IN ('tailored', 'approved', 'applied')
       AND b.status NOT IN ('tailored', 'approved', 'applied')
       AND same_posting(a.norm_company, a.norm_title, a.norm_city, a.description,
                        b.norm_company, b.norm_title, b.norm_city, b.description)
     ORDER BY a.id, b.id
  LOOP
    CONTINUE WHEN NOT EXISTS (SELECT 1 FROM jobs WHERE id = r.keep_id)
               OR NOT EXISTS (SELECT 1 FROM jobs WHERE id = r.drop_id);
    UPDATE job_sources SET job_id = r.keep_id WHERE job_id = r.drop_id;
    UPDATE jobs k SET
      description = CASE WHEN coalesce(length(k.description), 0) < coalesce(length(d.description), 0)
                         THEN d.description ELSE k.description END,
      canonical_url = coalesce(d.canonical_url, k.canonical_url),
      salary_min = coalesce(k.salary_min, d.salary_min), salary_max = coalesce(k.salary_max, d.salary_max),
      salary_currency = coalesce(k.salary_currency, d.salary_currency),
      deadline = coalesce(k.deadline, d.deadline), posted_at = coalesce(k.posted_at, d.posted_at),
      last_seen = greatest(k.last_seen, d.last_seen)
      FROM jobs d WHERE k.id = r.keep_id AND d.id = r.drop_id;
    DELETE FROM jobs WHERE id = r.drop_id;
    n := n + 1;
  END LOOP;
  RETURN n;
END;
$$;

SELECT merge_duplicate_jobs();
UPDATE jobs SET fingerprint = job_fingerprint(company, title, location);

-- upsert_job: layer 3 now uses same_posting() (everything else is unchanged from 001).
CREATE OR REPLACE FUNCTION upsert_job(
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

  -- 3. same company + near-identical title; city must agree, be coarse, or the descriptions match
  IF v_job_id IS NULL THEN
    SELECT j.id INTO v_job_id FROM jobs j
     WHERE j.norm_company = v_nc
       AND same_posting(j.norm_company, j.norm_title, j.norm_city, j.description,
                        v_nc, v_nt, v_ncity, p_description)
     ORDER BY similarity(j.norm_title, v_nt) DESC, j.id LIMIT 1;
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
