-- jobsearch database schema (MVP)
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---------------------------------------------------------------
-- Normalisation helpers (used for cross-source dedupe fingerprints)
-- ---------------------------------------------------------------
CREATE OR REPLACE FUNCTION norm_company(t text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
  SELECT regexp_replace(
           regexp_replace(lower(coalesce(t, '')),
             '\m(pvt|private|ltd|limited|inc|llc|llp|corp|corporation|co)\M\.?', '', 'g'),
           '[^a-z0-9]+', '', 'g')
$$;

CREATE OR REPLACE FUNCTION norm_title(t text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
  SELECT btrim(regexp_replace(
           regexp_replace(lower(coalesce(t, '')), '\([^)]*\)', ' ', 'g'),
           '[^a-z0-9]+', ' ', 'g'))
$$;

CREATE OR REPLACE FUNCTION norm_city(t text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
  SELECT regexp_replace(lower(split_part(coalesce(t, ''), ',', 1)), '[^a-z0-9]+', '', 'g')
$$;

-- ---------------------------------------------------------------
-- Canonical jobs (one row per real-world posting)
-- ---------------------------------------------------------------
CREATE TABLE jobs (
  id             bigserial PRIMARY KEY,
  fingerprint    text NOT NULL UNIQUE,      -- md5(company|title|city)
  title          text NOT NULL,
  company        text NOT NULL,
  location       text,
  is_remote      boolean,
  posted_at      timestamptz,
  description    text,
  salary_min     numeric,
  salary_max     numeric,
  salary_currency text,
  canonical_url  text,                      -- best apply link seen so far
  norm_title     text NOT NULL,
  norm_company   text NOT NULL,
  norm_city      text NOT NULL,
  desc_hash      text,
  -- pipeline state
  status         text NOT NULL DEFAULT 'new'
                 CHECK (status IN ('new','filtered_out','scored','shortlisted',
                                   'tailored','approved','applied','skipped','expired')),
  filter_reason  text,
  score          int,
  score_reason   text,
  scored_at      timestamptz,
  first_seen     timestamptz NOT NULL DEFAULT now(),
  last_seen      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX jobs_status_idx       ON jobs (status);
CREATE INDEX jobs_desc_hash_idx    ON jobs (desc_hash);
CREATE INDEX jobs_company_city_idx ON jobs (norm_company, norm_city);
CREATE INDEX jobs_norm_title_trgm  ON jobs USING gin (norm_title gin_trgm_ops);

-- Every source/listing that points at a canonical job
CREATE TABLE job_sources (
  id            bigserial PRIMARY KEY,
  job_id        bigint NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  source        text NOT NULL,              -- linkedin, indeed, naukri, greenhouse...
  source_job_id text NOT NULL,
  url           text,
  apply_url     text,
  raw_json      jsonb,
  first_seen    timestamptz NOT NULL DEFAULT now(),
  last_seen     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (source, source_job_id)
);
CREATE INDEX job_sources_job_idx ON job_sources (job_id);

-- ---------------------------------------------------------------
-- Dedupe + upsert. Layers: exact (source,id) -> fingerprint ->
-- fuzzy title (pg_trgm) at same company+city -> description hash.
-- Returns the canonical job id and whether it was newly created.
-- ---------------------------------------------------------------
CREATE OR REPLACE FUNCTION upsert_job(
  p_source text, p_source_job_id text, p_url text, p_apply_url text,
  p_title text, p_company text, p_location text, p_is_remote boolean,
  p_posted_at timestamptz, p_description text,
  p_salary_min numeric, p_salary_max numeric, p_salary_currency text,
  p_raw jsonb
) RETURNS TABLE (job_id bigint, is_new boolean)
LANGUAGE plpgsql AS $$
DECLARE
  v_job_id bigint;
  v_nt text := norm_title(p_title);
  v_nc text := norm_company(p_company);
  v_ncity text := norm_city(p_location);
  v_fp text := md5(v_nc || '|' || v_nt || '|' || v_ncity);
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
                      norm_title, norm_company, norm_city, desc_hash)
    VALUES (v_fp, p_title, p_company, p_location, p_is_remote, p_posted_at,
            p_description, p_salary_min, p_salary_max, p_salary_currency,
            coalesce(p_apply_url, p_url), v_nt, v_nc, v_ncity, v_dh)
    RETURNING id INTO v_job_id;
    v_new := true;
  ELSE
    -- known job: refresh liveness; prefer a direct (non-board) apply link; fill gaps
    UPDATE jobs SET
      last_seen   = now(),
      status      = CASE WHEN status = 'expired' THEN 'new' ELSE status END,
      description = CASE WHEN coalesce(length(description),0) < coalesce(length(p_description),0)
                         THEN p_description ELSE description END,
      canonical_url = CASE WHEN p_apply_url IS NOT NULL THEN p_apply_url ELSE canonical_url END,
      salary_min  = coalesce(salary_min, p_salary_min),
      salary_max  = coalesce(salary_max, p_salary_max),
      salary_currency = coalesce(salary_currency, p_salary_currency)
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
-- Search profile = what to search for + free rule filters
-- ---------------------------------------------------------------
CREATE TABLE search_profile (
  id                 serial PRIMARY KEY,
  name               text NOT NULL UNIQUE,
  active             boolean NOT NULL DEFAULT true,
  search_terms       text[] NOT NULL DEFAULT '{}',
  locations          text[] NOT NULL DEFAULT '{}',   -- scrape locations
  sources            text[] NOT NULL DEFAULT '{linkedin,indeed}',
  allow_remote       boolean NOT NULL DEFAULT true,
  min_yoe            int,                            -- reject jobs requiring less than this
  max_yoe            int,                            -- reject jobs requiring more than this
  include_keywords   text[] NOT NULL DEFAULT '{}',   -- at least one must appear (if non-empty)
  exclude_keywords   text[] NOT NULL DEFAULT '{}',   -- any match rejects
  exclude_companies  text[] NOT NULL DEFAULT '{}',
  max_age_days       int NOT NULL DEFAULT 7,
  score_threshold    int NOT NULL DEFAULT 70,
  updated_at         timestamptz NOT NULL DEFAULT now()
);

-- Placeholder profile: edit these values before the first real run.
INSERT INTO search_profile (name, search_terms, locations, min_yoe, max_yoe,
                            include_keywords, exclude_keywords, max_age_days, score_threshold)
VALUES ('default', '{"CHANGE ME role title"}', '{"CHANGE ME city"}', 0, 10,
        '{}', '{"intern","unpaid"}', 7, 70);

-- ---------------------------------------------------------------
-- Applications (tailored artifacts + status tracking)
-- ---------------------------------------------------------------
CREATE TABLE applications (
  id                 bigserial PRIMARY KEY,
  job_id             bigint NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE CASCADE,
  status             text NOT NULL DEFAULT 'drafted'
                     CHECK (status IN ('drafted','approved','applied','in_progress',
                                       'interview','offer','rejected','withdrawn')),
  resume_path        text,
  cover_letter_path  text,
  tailor_patch       jsonb,
  applied_at         timestamptz,
  notes              text,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now()
);
