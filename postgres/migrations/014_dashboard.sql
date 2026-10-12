-- 014: the web dashboard, version 1 (Phase 9, see docs/dashboard.md).
--
--  1. v_dashboard_jobs: one row per job for the table. pipeline_status is the application's status when there is
--     one, because set_application_status() leaves jobs.status at 'applied' for every later stage (interview,
--     offer, rejected, ...). It also lists the boards a job was found on and one http(s) apply link.
--  2. v_dashboard_job_detail and v_dashboard_job_sources: the extra fields of the details panel. Neither exposes
--     the stored file paths or the raw scraped payloads.
--  3. dashboard_summary(): the cards, the donut and the funnel in one call.
--  4. dashboard_set_status(): set_application_status() plus a "changed elsewhere" check and a refusal for
--     scam-blocked jobs. dashboard_application_file(): the stored PDF path of one job, the only way the dashboard
--     sees a path.
--  5. jobsearch_dashboard: the role the dashboard connects as. It can read the three views and run the three
--     functions and nothing else; the functions are SECURITY DEFINER, so it never needs table privileges. It is
--     created NOLOGIN: the dashboard-db-init service gives it a password from .env.
--  Nothing existing is changed.
--
-- The functions raise their own SQLSTATEs so the API can map them without parsing messages:
--   JD404 no such job, JD403 the job is scam-blocked, JD409 the status changed elsewhere (DETAIL holds the
--   current status), JD422 bad input.

-- ---------------------------------------------------------------- 1. the table
CREATE VIEW v_dashboard_jobs AS
SELECT j.id, j.title, j.company, j.location, j.is_remote,
       j.salary_min, j.salary_max, j.salary_currency,
       j.score, j.verdict, j.scores, j.scam_risk,
       j.status AS job_status, j.filter_reason,
       j.posted_at, j.first_seen, j.last_seen, j.deadline,
       (j.deadline IS NOT NULL AND j.deadline BETWEEN current_date AND current_date + 7) AS closing_soon,
       a.status AS application_status, a.applied_at,
       coalesce(a.needs_attention, false) AS needs_attention,
       (a.resume_path IS NOT NULL)        AS has_resume,
       (a.cover_letter_path IS NOT NULL)  AS has_cover_letter,
       CASE WHEN j.status = 'scam_blocked' THEN 'scam_blocked'
            ELSE coalesce(a.status, j.status) END AS pipeline_status,
       coalesce(src.sources, '{}'::text[]) AS sources,
       -- canonical_url is the best apply link upsert_job has seen; a listing's own link is the fallback.
       -- Posting links come from the internet: only http(s) ones are offered.
       CASE WHEN j.canonical_url ~* '^https?://' THEN j.canonical_url ELSE src.first_link END AS apply_url
FROM jobs j
LEFT JOIN applications a ON a.job_id = j.id
LEFT JOIN LATERAL (
  SELECT array_agg(DISTINCT s.source ORDER BY s.source) AS sources,
         (SELECT l.link
            FROM job_sources s2
            CROSS JOIN LATERAL (VALUES (1, s2.apply_url), (2, s2.url)) AS l (pref, link)
           WHERE s2.job_id = j.id AND l.link ~* '^https?://'
           ORDER BY s2.first_seen, s2.id, l.pref
           LIMIT 1) AS first_link
    FROM job_sources s
   WHERE s.job_id = j.id
) src ON true;

-- ---------------------------------------------------------------- 2. the details panel
CREATE VIEW v_dashboard_job_detail AS
SELECT v.*,
       j.description, j.canonical_url, j.score_reason, j.strengths, j.gaps, j.gates,
       j.yoe_min, j.yoe_max, j.scam_score, j.scam_flags, j.posting_language,
       a.variant, a.notes, a.review_note, a.reviewed_at, a.attention_reason, a.cost_usd, a.model
FROM v_dashboard_jobs v
JOIN jobs j ON j.id = v.id
LEFT JOIN applications a ON a.job_id = v.id;

CREATE VIEW v_dashboard_job_sources AS
SELECT job_id, source, url, apply_url, first_seen, last_seen
FROM job_sources;

-- ---------------------------------------------------------------- 3. cards, donut, funnel
-- "applied" counts everything ever submitted (applied_at is kept when a stage moves on); the per-stage numbers
-- are the CURRENT status, so a job that went from interview to rejected counts as rejected only.
-- The funnel's steps are nested: each one counts only jobs that also made it through every step before it, so a
-- step is never larger than the one above it. This matters because the scorer also scores jobs that the filters
-- then exclude (filtered_out with a score and the verdict 'excluded'): without the nesting they would count as
-- scored and as above the threshold. The shortlisted step is "scored and at or above today's threshold of the
-- 'default' profile (the one the scraper reads), or already acted on"; a job you applied to or skipped by hand
-- belongs to the shortlist even if the threshold was raised afterwards.
CREATE FUNCTION dashboard_summary() RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  WITH t AS (
    SELECT (SELECT score_threshold FROM search_profile WHERE name = 'default') AS threshold
  ), c AS (
    SELECT count(*)                                                              AS found,
           count(*) FILTER (WHERE v.job_status NOT IN ('filtered_out', 'scam_blocked')) AS passed_filters,
           count(*) FILTER (WHERE v.job_status NOT IN ('filtered_out', 'scam_blocked')
                              AND v.score IS NOT NULL)                           AS scored,
           count(*) FILTER (WHERE v.job_status NOT IN ('filtered_out', 'scam_blocked')
                              AND v.score IS NOT NULL
                              AND (v.score >= t.threshold OR v.application_status IS NOT NULL)) AS shortlisted_step,
           count(*) FILTER (WHERE v.job_status NOT IN ('filtered_out', 'scam_blocked')
                              AND v.score IS NOT NULL
                              AND (v.score >= t.threshold OR v.application_status IS NOT NULL)
                              AND v.applied_at IS NOT NULL)                      AS applied_step,
           count(*) FILTER (WHERE v.job_status NOT IN ('filtered_out', 'scam_blocked')
                              AND v.score IS NOT NULL
                              AND (v.score >= t.threshold OR v.application_status IS NOT NULL)
                              AND v.applied_at IS NOT NULL
                              AND v.application_status IN ('interview', 'offer')) AS interview_step,
           count(*) FILTER (WHERE v.job_status NOT IN ('filtered_out', 'scam_blocked')
                              AND v.score IS NOT NULL
                              AND (v.score >= t.threshold OR v.application_status IS NOT NULL)
                              AND v.applied_at IS NOT NULL
                              AND v.application_status = 'offer')                AS offer_step,
           count(*) FILTER (WHERE v.pipeline_status = 'shortlisted')             AS shortlisted,
           count(*) FILTER (WHERE v.pipeline_status = 'shortlisted'
                              AND v.first_seen >= now() - interval '7 days')     AS new_this_week,
           count(*) FILTER (WHERE v.applied_at IS NOT NULL)                      AS applied,
           count(*) FILTER (WHERE v.application_status = 'applied')              AS waiting,
           count(*) FILTER (WHERE v.application_status = 'in_progress')          AS in_progress,
           count(*) FILTER (WHERE v.application_status = 'interview')            AS interview,
           count(*) FILTER (WHERE v.application_status = 'offer')                AS offer,
           count(*) FILTER (WHERE v.application_status = 'rejected')             AS rejected,
           count(*) FILTER (WHERE v.application_status = 'withdrawn')            AS withdrawn
      FROM v_dashboard_jobs v CROSS JOIN t
  )
  SELECT jsonb_build_object(
    'threshold', t.threshold,
    'cards', jsonb_build_object(
      'found', c.found, 'shortlisted', c.shortlisted, 'new_this_week', c.new_this_week, 'applied', c.applied,
      'in_progress', c.in_progress, 'interview', c.interview, 'offer', c.offer, 'rejected', c.rejected,
      'withdrawn', c.withdrawn),
    'by_status', jsonb_build_object(
      'applied', c.waiting, 'in_progress', c.in_progress, 'interview', c.interview, 'offer', c.offer,
      'rejected', c.rejected, 'withdrawn', c.withdrawn, 'not_applied', c.shortlisted),
    'funnel', jsonb_build_array(
      jsonb_build_object('step', 'found',          'count', c.found),
      jsonb_build_object('step', 'passed_filters', 'count', c.passed_filters),
      jsonb_build_object('step', 'scored',         'count', c.scored),
      jsonb_build_object('step', 'shortlisted',    'count', c.shortlisted_step),
      jsonb_build_object('step', 'applied',        'count', c.applied_step),
      jsonb_build_object('step', 'interview',      'count', c.interview_step),
      jsonb_build_object('step', 'offer',          'count', c.offer_step)),
    'generated_at', now())
  FROM c CROSS JOIN t
$$;

-- ---------------------------------------------------------------- 4. the one write, and file paths
-- The browser sends the status it showed (p_expected, the view's pipeline_status). The job row is locked first, so
-- the check and the write cannot interleave with another dashboard request. An overlapping CLI or Telegram change
-- that has not committed yet is not seen by the check; set_application_status() then waits for it and writes last.
CREATE FUNCTION dashboard_set_status(p_job_id bigint, p_status text, p_expected text, p_note text DEFAULT NULL)
RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v_job text; v_app text; v_current text; v_note text := nullif(btrim(p_note), '');
BEGIN
  IF p_status IS NULL OR p_status NOT IN ('approved', 'skipped', 'applied', 'in_progress', 'interview', 'offer', 'rejected', 'withdrawn') THEN
    RAISE EXCEPTION 'unknown status %', p_status USING ERRCODE = 'JD422';
  END IF;
  IF length(v_note) > 500 THEN
    RAISE EXCEPTION 'note is longer than 500 characters' USING ERRCODE = 'JD422';
  END IF;
  SELECT status INTO v_job FROM jobs WHERE id = p_job_id FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'no job %', p_job_id USING ERRCODE = 'JD404';
  END IF;
  IF v_job = 'scam_blocked' THEN
    RAISE EXCEPTION 'job % is scam-blocked', p_job_id USING ERRCODE = 'JD403';
  END IF;
  SELECT status INTO v_app FROM applications WHERE job_id = p_job_id FOR UPDATE;
  v_current := coalesce(v_app, v_job);
  IF v_current IS DISTINCT FROM p_expected THEN
    RAISE EXCEPTION 'job % changed', p_job_id USING ERRCODE = 'JD409', DETAIL = v_current;
  END IF;
  RETURN set_application_status(p_job_id, p_status, v_note);
END;
$$;

CREATE FUNCTION dashboard_application_file(p_job_id bigint, p_kind text) RETURNS text
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
  IF p_kind IS NULL OR p_kind NOT IN ('resume', 'cover_letter') THEN
    RAISE EXCEPTION 'unknown file kind %', p_kind USING ERRCODE = 'JD422';
  END IF;
  RETURN (SELECT CASE p_kind WHEN 'resume' THEN resume_path ELSE cover_letter_path END
            FROM applications WHERE job_id = p_job_id);
END;
$$;

-- ---------------------------------------------------------------- 5. the dashboard's role
-- Roles belong to the whole server, not one database, hence IF NOT EXISTS (the SQL tests apply this migration to
-- two throwaway databases on the same server).
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'jobsearch_dashboard') THEN
    CREATE ROLE jobsearch_dashboard NOLOGIN CONNECTION LIMIT 10;
  END IF;
END $$;
ALTER ROLE jobsearch_dashboard SET statement_timeout = '10s';
ALTER ROLE jobsearch_dashboard SET idle_in_transaction_session_timeout = '30s';

DO $$ BEGIN EXECUTE format('GRANT CONNECT ON DATABASE %I TO jobsearch_dashboard', current_database()); END $$;
GRANT USAGE ON SCHEMA public TO jobsearch_dashboard;
GRANT SELECT ON v_dashboard_jobs, v_dashboard_job_detail, v_dashboard_job_sources TO jobsearch_dashboard;

-- Every role may run a new function unless told otherwise; these three are for the dashboard only.
REVOKE ALL ON FUNCTION dashboard_summary(), dashboard_set_status(bigint, text, text, text),
                       dashboard_application_file(bigint, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION dashboard_summary(), dashboard_set_status(bigint, text, text, text),
                          dashboard_application_file(bigint, text) TO jobsearch_dashboard;
