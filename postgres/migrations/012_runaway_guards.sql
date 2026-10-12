-- 012: guards against runaway spending and loops.
--
--  1. Every attempt to process a job is COUNTED when it is claimed (score_claims / tailor_claims), not only
--     when it ends in a bad reply. Before this, a job whose reply was never recorded (API outage, a crash
--     after the reply) could be re-claimed and re-paid for every lease period, forever. Now it is claimed a
--     bounded number of times and then PARKED, visibly, until you re-queue it.
--     An attempt that provably cost nothing (the API was down, the budget was paused) is refunded, so an
--     outage never parks a healthy job.
--  2. Apify gets its own monthly and daily caps (it only had a per-run cap and the account's own credit).
--  3. A global pause switch, separate from the per-stage settings, as an emergency brake.

-- ---------------------------------------------------------------- 1. counted claims
ALTER TABLE jobs
  ADD COLUMN score_claims  int NOT NULL DEFAULT 0,
  ADD COLUMN tailor_claims int NOT NULL DEFAULT 0;

-- a new parameter would create an overload that makes one-argument calls ambiguous, so replace the functions
DROP FUNCTION IF EXISTS claim_jobs_to_score(int, int, interval);
CREATE FUNCTION claim_jobs_to_score(
  p_limit int, p_max_attempts int DEFAULT 3, p_lease interval DEFAULT interval '30 minutes', p_max_claims int DEFAULT 6
) RETURNS TABLE (id bigint, title text, company text, location text, posted_at timestamptz, description text)
LANGUAGE sql AS $$
  UPDATE jobs j SET scoring_claimed_at = now(), score_claims = j.score_claims + 1
   WHERE j.id IN (
     SELECT c.id FROM jobs c
      WHERE c.status = 'new'
        AND coalesce(c.scam_risk, 'low') <> 'high'
        AND c.score_attempts < p_max_attempts
        AND c.score_claims < p_max_claims
        AND (c.scoring_claimed_at IS NULL OR c.scoring_claimed_at < now() - p_lease)
      ORDER BY c.posted_at DESC NULLS LAST, c.id DESC
      LIMIT p_limit
      FOR UPDATE SKIP LOCKED)
  RETURNING j.id, j.title, j.company, j.location, j.posted_at, j.description
$$;

DROP FUNCTION IF EXISTS claim_jobs_to_tailor(int, int, int, interval);
CREATE FUNCTION claim_jobs_to_tailor(
  p_limit int, p_min_score int, p_max_attempts int DEFAULT 2, p_lease interval DEFAULT interval '20 minutes', p_max_claims int DEFAULT 3
) RETURNS TABLE (id bigint, title text, company text, location text, description text, canonical_url text,
                 score int, verdict text, strengths jsonb, gaps jsonb, gates jsonb, posted_at timestamptz)
LANGUAGE sql AS $$
  UPDATE jobs j SET tailoring_claimed_at = now(), tailor_claims = j.tailor_claims + 1
   WHERE j.id IN (
     SELECT c.id FROM jobs c
      WHERE c.status = 'shortlisted'
        AND coalesce(c.scam_risk, 'low') <> 'high'
        AND c.score >= p_min_score
        AND c.tailor_attempts < p_max_attempts
        AND c.tailor_claims < p_max_claims
        AND (c.tailoring_claimed_at IS NULL OR c.tailoring_claimed_at < now() - p_lease)
        AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.job_id = c.id)
      ORDER BY c.score DESC, c.id
      LIMIT p_limit
      FOR UPDATE SKIP LOCKED)
  RETURNING j.id, j.title, j.company, j.location, j.description, j.canonical_url, j.score, j.verdict,
            j.strengths, j.gaps, j.gates, j.posted_at
$$;

-- Give back a claim that cost nothing (the API was down, the budget was paused): the job is not penalised.
CREATE FUNCTION release_score_claim(p_job_id bigint) RETURNS int LANGUAGE sql AS $$
  UPDATE jobs SET score_claims = greatest(score_claims - 1, 0), scoring_claimed_at = NULL
   WHERE id = p_job_id RETURNING score_claims
$$;
CREATE FUNCTION release_tailor_claim(p_job_id bigint) RETURNS int LANGUAGE sql AS $$
  UPDATE jobs SET tailor_claims = greatest(tailor_claims - 1, 0), tailoring_claimed_at = NULL
   WHERE id = p_job_id RETURNING tailor_claims
$$;

-- Jobs that ran out of attempts, for the doctor and the status line. 'score' parks jobs that are still `new`;
-- 'tailor' parks shortlisted jobs that never got an application.
CREATE VIEW v_parked_jobs AS
SELECT 'score' AS stage, j.id AS job_id, j.title, j.company, j.score_claims AS claims, j.score_attempts AS strikes
  FROM jobs j
 WHERE j.status = 'new' AND coalesce(j.scam_risk, 'low') <> 'high' AND (j.score_claims >= 6 OR j.score_attempts >= 3)
UNION ALL
SELECT 'tailor', j.id, j.title, j.company, j.tailor_claims, j.tailor_attempts
  FROM jobs j
 WHERE j.status = 'shortlisted' AND coalesce(j.scam_risk, 'low') <> 'high'
   AND (j.tailor_claims >= 3 OR j.tailor_attempts >= 2)
   AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.job_id = j.id);

-- Put parked jobs back in the queue (one job, or every parked job of that stage). Returns how many.
CREATE FUNCTION requeue_jobs(p_stage text, p_job_id bigint DEFAULT NULL) RETURNS int LANGUAGE plpgsql AS $$
DECLARE n int;
BEGIN
  IF p_stage = 'score' THEN
    UPDATE jobs SET score_claims = 0, score_attempts = 0, scoring_claimed_at = NULL
     WHERE (p_job_id IS NULL OR id = p_job_id) AND id IN (SELECT job_id FROM v_parked_jobs WHERE stage = 'score');
  ELSIF p_stage = 'tailor' THEN
    UPDATE jobs SET tailor_claims = 0, tailor_attempts = 0, tailoring_claimed_at = NULL
     WHERE (p_job_id IS NULL OR id = p_job_id) AND id IN (SELECT job_id FROM v_parked_jobs WHERE stage = 'tailor');
  ELSE
    RAISE EXCEPTION 'stage must be score or tailor';
  END IF;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END;
$$;
-- ---------------------------------------------------------------- 2. Apify caps
ALTER TABLE budget_settings
  ADD COLUMN apify_monthly_cap_usd numeric NOT NULL DEFAULT 15  CHECK (apify_monthly_cap_usd >= 0),   -- the free credit of three accounts
  ADD COLUMN apify_daily_cap_usd   numeric NOT NULL DEFAULT 0.5 CHECK (apify_daily_cap_usd >= 0);     -- about monthly / 30: pace the credit over the month

-- Same shape as budget_status(), for provider = 'apify'. Spend is whatever was logged (the larger of the actor's
-- estimate and the real balance change, see apify_source.py).
CREATE FUNCTION apify_budget_status() RETURNS TABLE (
  month_spent numeric, day_spent numeric, monthly_cap numeric, daily_cap numeric,
  month_remaining numeric, day_remaining numeric, blocked boolean, blocked_reason text)
LANGUAGE sql STABLE AS $$
  WITH s AS (SELECT * FROM budget_settings WHERE id = 1),
  w AS (SELECT s.*,
               (date_trunc('month', now() AT TIME ZONE s.timezone)) AT TIME ZONE s.timezone AS m0,
               (date_trunc('day',   now() AT TIME ZONE s.timezone)) AT TIME ZONE s.timezone AS d0 FROM s),
  t AS (SELECT w.*,
               coalesce((SELECT sum(cost_usd) FROM llm_usage u WHERE u.provider = 'apify' AND u.at >= w.m0), 0) AS ms,
               coalesce((SELECT sum(cost_usd) FROM llm_usage u WHERE u.provider = 'apify' AND u.at >= w.d0), 0) AS ds FROM w)
  SELECT ms, ds, apify_monthly_cap_usd, apify_daily_cap_usd,
         greatest(apify_monthly_cap_usd - ms, 0), greatest(apify_daily_cap_usd - ds, 0),
         ms >= apify_monthly_cap_usd OR ds >= apify_daily_cap_usd,
         CASE WHEN ms >= apify_monthly_cap_usd THEN 'Apify monthly cap reached'
              WHEN ds >= apify_daily_cap_usd THEN 'Apify daily cap reached' END
    FROM t
$$;

-- ---------------------------------------------------------------- 3. global pause
CREATE TABLE schedule_control (
  id        int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  paused    boolean NOT NULL DEFAULT false,
  paused_at timestamptz,
  reason    text
);
INSERT INTO schedule_control DEFAULT VALUES;

-- While paused, nothing is claimed at all (not even "run now"): the one switch that stops every scheduled stage.
CREATE OR REPLACE FUNCTION claim_due_schedules(p_skip text[] DEFAULT '{}') RETURNS TABLE (name text, batch int, model text)
LANGUAGE sql AS $$
  WITH tz AS (SELECT timezone AS z FROM budget_settings WHERE id = 1),
       nowl AS (SELECT (now() AT TIME ZONE tz.z) AS t, tz.z FROM tz)
  UPDATE schedule_settings s SET last_run_at = now(), run_now = false
    FROM nowl
   WHERE NOT (SELECT paused FROM schedule_control WHERE id = 1)
     AND NOT (s.name = ANY (p_skip))
     AND ( s.run_now
        OR ( s.enabled AND (
               (s.every_minutes IS NOT NULL
                  AND (s.last_run_at IS NULL OR now() >= s.last_run_at + s.every_minutes * interval '1 minute'))
            OR (s.at_time IS NOT NULL
                  AND nowl.t::time >= s.at_time
                  AND (s.last_run_at IS NULL OR (s.last_run_at AT TIME ZONE nowl.z)::date < nowl.t::date)) )))
  RETURNING s.name, s.batch, s.model;
$$;
