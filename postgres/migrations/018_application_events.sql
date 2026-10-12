-- 018: the history of every application, for the dashboard's timeline, board and stats (docs/dashboard.md, version 3a).
--
--  1. application_events: one row each time an application's status changes (or a note is recorded without a
--     change): the old status, the new status, the note and when. A trigger on `applications` writes it, so every
--     path that changes a status (the dashboard, the CLI, Telegram, the tailoring pipeline) is recorded without any
--     of them being edited. `source` is 'dashboard' when the dashboard made the change, 'backfill' for the rows made
--     below from the applications that already exist, and NULL otherwise (the CLI, Telegram or the pipeline).
--  2. dashboard_job_events(job): the timeline of one job.
--  3. dashboard_board(): the Kanban board: the cards of every column, newest first (the best score first for jobs not
--     applied to yet), and the number in each.
--  4. dashboard_stats(): the response rate, the days to a first reply and the last 12 weeks.
--  5. dashboard_set_status() now marks its change as the dashboard's. Same arguments, same checks, same result.
--  The functions are SECURITY DEFINER and granted to jobsearch_dashboard only, like those of migrations 014 and 016:
--  the role still has no table privileges.
--
-- A note that is only cleared is not an event: tailoring a job again writes review_note = NULL on the same status.
-- What "a reply" means: the first move after 'applied' to in_progress, interview, offer or rejected. 'withdrawn' is
-- your own decision, not an answer, and 'skipped' is not an application at all.

-- ---------------------------------------------------------------- 1. the history
CREATE TABLE application_events (
  id          bigserial PRIMARY KEY,
  job_id      bigint NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  from_status text,                                 -- NULL for the first event of an application
  to_status   text NOT NULL,
  note        text,
  source      text CHECK (source IN ('dashboard', 'backfill')),
  at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX application_events_job_idx ON application_events (job_id, at, id);
CREATE INDEX application_events_at_idx  ON application_events (at);

CREATE FUNCTION record_application_event() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE v_source text := nullif(current_setting('jobsearch.source', true), '');
BEGIN
  IF TG_OP = 'INSERT' THEN
    INSERT INTO application_events (job_id, from_status, to_status, note, source)
    VALUES (NEW.job_id, NULL, NEW.status, NEW.review_note, CASE WHEN v_source = 'dashboard' THEN v_source END);
  ELSE
    -- a note recorded without a status change is an event too (from and to are the same)
    INSERT INTO application_events (job_id, from_status, to_status, note, source)
    VALUES (NEW.job_id, OLD.status, NEW.status,
            CASE WHEN NEW.review_note IS DISTINCT FROM OLD.review_note THEN NEW.review_note END,
            CASE WHEN v_source = 'dashboard' THEN v_source END);
  END IF;
  RETURN NULL;
END;
$$;

CREATE TRIGGER applications_record_event_insert AFTER INSERT ON applications
  FOR EACH ROW EXECUTE FUNCTION record_application_event();
CREATE TRIGGER applications_record_event_update AFTER UPDATE OF status, review_note ON applications
  FOR EACH ROW WHEN (OLD.status IS DISTINCT FROM NEW.status
                     OR (OLD.review_note IS DISTINCT FROM NEW.review_note AND NEW.review_note IS NOT NULL))
  EXECUTE FUNCTION record_application_event();

-- What is already there. Only the current status is known, so: when it was submitted (if it was), and where it
-- stands now. The 'backfill' rows are left out of the reply statistics. An application that was submitted and then
-- moved back (skipped, approved) still has applied_at, so it gets the 'applied' row too.
INSERT INTO application_events (job_id, from_status, to_status, note, source, at)
SELECT a.job_id, NULL, 'applied', CASE WHEN a.status = 'applied' THEN a.review_note END, 'backfill', a.applied_at
  FROM applications a
 WHERE a.applied_at IS NOT NULL;
INSERT INTO application_events (job_id, from_status, to_status, note, source, at)
SELECT a.job_id,
       CASE WHEN a.applied_at IS NOT NULL THEN 'applied' END,
       a.status, a.review_note, 'backfill',
       CASE WHEN a.applied_at IS NOT NULL THEN greatest(a.updated_at, a.applied_at)
            ELSE coalesce(a.reviewed_at, a.updated_at) END
  FROM applications a
 WHERE NOT (a.applied_at IS NOT NULL AND a.status = 'applied');   -- the 'applied' row above already says it

-- ---------------------------------------------------------------- 2. one job's timeline
CREATE FUNCTION dashboard_job_events(p_job_id bigint) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM jobs WHERE id = p_job_id) THEN
    RAISE EXCEPTION 'no job %', p_job_id USING ERRCODE = 'JD404';
  END IF;
  RETURN coalesce((SELECT jsonb_agg(jsonb_build_object(
            'id', e.id, 'from_status', e.from_status, 'to_status', e.to_status, 'note', e.note,
            'source', e.source, 'at', e.at) ORDER BY e.at, e.id)
          FROM (SELECT * FROM application_events WHERE job_id = p_job_id ORDER BY at DESC, id DESC LIMIT 200) e), '[]');
END;
$$;

-- ---------------------------------------------------------------- 3. the board
-- Columns: not_applied (shortlisted, tailored, drafted and approved jobs: ones you have not submitted), then the
-- stages after submission. withdrawn and skipped jobs are counted, not shown. Each column carries its true total and
-- at most 40 cards. status_since is when the job last changed status (a note does not count); for a job not applied
-- to yet it is when it was scored (or first seen).
CREATE FUNCTION dashboard_board() RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  WITH cards AS (
    SELECT v.id, v.title, v.company, v.location, v.is_remote, v.score, v.pipeline_status,
           CASE WHEN v.pipeline_status IN ('shortlisted', 'tailored', 'drafted', 'approved') THEN 'not_applied'
                WHEN v.pipeline_status IN ('applied', 'in_progress', 'interview', 'offer', 'rejected') THEN v.pipeline_status
           END AS col,
           v.applied_at, v.deadline, v.closing_soon, v.has_resume, v.has_cover_letter, v.needs_attention,
           -- not applied yet: when it was scored. Otherwise: the last real change of status (a note does not count)
           CASE WHEN v.pipeline_status IN ('shortlisted', 'tailored', 'drafted', 'approved')
                THEN coalesce(j.scored_at, v.first_seen)
                ELSE coalesce((SELECT max(e.at) FROM application_events e
                                WHERE e.job_id = v.id AND e.from_status IS DISTINCT FROM e.to_status),
                              v.applied_at, v.first_seen) END AS status_since
      FROM v_dashboard_jobs v
      JOIN jobs j ON j.id = v.id
  ), ranked AS (
    SELECT c.*, row_number() OVER (
             PARTITION BY c.col
             ORDER BY CASE WHEN c.col = 'not_applied' THEN c.score END DESC NULLS LAST, c.status_since DESC, c.id DESC) AS rn
      FROM cards c
     WHERE c.col IS NOT NULL
  ), cols(ord, key) AS (
    VALUES (1, 'not_applied'), (2, 'applied'), (3, 'in_progress'), (4, 'interview'), (5, 'offer'), (6, 'rejected')
  )
  SELECT jsonb_build_object(
    'generated_at', now(),
    'columns', (SELECT jsonb_agg(jsonb_build_object(
        'key', cols.key,
        'total', (SELECT count(*) FROM ranked r WHERE r.col = cols.key),
        'items', coalesce((SELECT jsonb_agg(jsonb_build_object(
              'id', r.id, 'title', r.title, 'company', r.company, 'location', r.location, 'is_remote', r.is_remote,
              'score', r.score, 'pipeline_status', r.pipeline_status, 'applied_at', r.applied_at,
              'deadline', r.deadline, 'closing_soon', r.closing_soon, 'has_resume', r.has_resume,
              'has_cover_letter', r.has_cover_letter, 'needs_attention', r.needs_attention,
              'status_since', r.status_since) ORDER BY r.rn)
            FROM ranked r WHERE r.col = cols.key AND r.rn <= 40), '[]'))
        ORDER BY cols.ord) FROM cols),
    'closed', jsonb_build_object(
        'withdrawn', (SELECT count(*) FROM cards WHERE pipeline_status = 'withdrawn'),
        'skipped',   (SELECT count(*) FROM cards WHERE pipeline_status = 'skipped')))
$$;

-- ---------------------------------------------------------------- 4. the numbers
-- Weeks start on Monday in the configured time zone (budget_settings, like the spend history). A job counts as
-- found in the week it was first seen, as shortlisted in the week it was scored at or above today's threshold,
-- and as applied in the week it first reached a submitted status. Every figure counts a job once, and only jobs that
-- were ever submitted (applications.applied_at, as on the Jobs page); a mistaken move stays in the history, so a job
-- dropped on Offer by mistake and moved back still counts as having reached it (see docs/dashboard.md, known limits).
CREATE FUNCTION dashboard_stats() RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  WITH tz AS (
    SELECT timezone AS z FROM budget_settings WHERE id = 1
  ), th AS (
    SELECT (SELECT score_threshold FROM search_profile WHERE name = 'default') AS threshold
  ), submitted AS (
    -- the same meaning as the Jobs page's "Applied" card: ever submitted (applied_at is kept when a job moves on or back)
    SELECT job_id FROM applications WHERE applied_at IS NOT NULL
  ), first_submitted AS (
    -- the first time it reached any submitted status (a job moved straight to interview was submitted then)
    SELECT e.job_id, min(e.at) AS at FROM application_events e JOIN submitted s ON s.job_id = e.job_id
     WHERE e.to_status IN ('applied', 'in_progress', 'interview', 'offer', 'rejected', 'withdrawn') GROUP BY e.job_id
  ), real_applied AS (
    -- reply times start from a real, recorded 'applied': the backfilled rows do not know the true moment
    SELECT job_id, min(at) AS at FROM application_events
     WHERE to_status = 'applied' AND source IS DISTINCT FROM 'backfill' GROUP BY job_id
  ), reply_days AS (
    SELECT extract(epoch FROM min(e.at) - a.at) / 86400.0 AS days
      FROM application_events e JOIN real_applied a ON a.job_id = e.job_id AND e.at > a.at
     WHERE e.to_status IN ('in_progress', 'interview', 'offer', 'rejected') AND e.source IS DISTINCT FROM 'backfill'
     GROUP BY e.job_id, a.at
  ), reached AS (
    SELECT e.job_id, bool_or(e.to_status IN ('in_progress', 'interview', 'offer', 'rejected')) AS replied,
           bool_or(e.to_status IN ('interview', 'offer')) AS interview, bool_or(e.to_status = 'offer') AS offer
      FROM application_events e JOIN submitted s ON s.job_id = e.job_id GROUP BY e.job_id
  ), weeks AS (
    SELECT (date_trunc('week', now() AT TIME ZONE tz.z) - make_interval(weeks => n))::date AS week_start
      FROM tz, generate_series(0, 11) AS n
  ), first_week AS (
    SELECT min(week_start) AS w FROM weeks
  ), found_w AS (
    SELECT date_trunc('week', j.first_seen AT TIME ZONE tz.z)::date AS week_start, count(*) AS n
      FROM jobs j, tz, first_week f
     WHERE j.status <> 'scam_blocked' AND (j.first_seen AT TIME ZONE tz.z)::date >= f.w
     GROUP BY 1
  ), shortlisted_w AS (
    SELECT date_trunc('week', j.scored_at AT TIME ZONE tz.z)::date AS week_start, count(*) AS n
      FROM jobs j, tz, first_week f, th
     WHERE j.scored_at IS NOT NULL AND j.score >= th.threshold AND j.status NOT IN ('filtered_out', 'scam_blocked')
       AND (j.scored_at AT TIME ZONE tz.z)::date >= f.w
     GROUP BY 1
  ), applied_w AS (
    SELECT date_trunc('week', f.at AT TIME ZONE tz.z)::date AS week_start, count(*) AS n
      FROM first_submitted f, tz, first_week fw
     WHERE (f.at AT TIME ZONE tz.z)::date >= fw.w
     GROUP BY 1
  )
  SELECT jsonb_build_object(
    'generated_at', now(),
    'timezone', (SELECT z FROM tz),
    'applied', (SELECT count(*) FROM submitted),
    'responded', (SELECT count(*) FROM reached WHERE replied),
    'reached_interview', (SELECT count(*) FROM reached WHERE interview),
    'reached_offer', (SELECT count(*) FROM reached WHERE offer),
    'avg_days_to_reply', (SELECT round(avg(days)::numeric, 1) FROM reply_days),
    'replies_measured', (SELECT count(*) FROM reply_days),
    'weekly', (SELECT jsonb_agg(jsonb_build_object(
                  'week_start', w.week_start, 'found', coalesce(fw.n, 0), 'shortlisted', coalesce(sw.n, 0),
                  'applied', coalesce(aw.n, 0)) ORDER BY w.week_start)
                 FROM weeks w
                 LEFT JOIN found_w fw ON fw.week_start = w.week_start
                 LEFT JOIN shortlisted_w sw ON sw.week_start = w.week_start
                 LEFT JOIN applied_w aw ON aw.week_start = w.week_start))
$$;

-- ---------------------------------------------------------------- 5. the dashboard's own status change, marked
-- The same function as in 014 (same arguments, same checks, same result), with one added line: it tells the trigger
-- that this change comes from the dashboard. The marker lives for this transaction only.
CREATE OR REPLACE FUNCTION dashboard_set_status(p_job_id bigint, p_status text, p_expected text, p_note text DEFAULT NULL)
RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v_job text; v_app text; v_current text; v_note text := nullif(btrim(p_note), ''); v_result text;
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
  PERFORM set_config('jobsearch.source', 'dashboard', true);
  v_result := set_application_status(p_job_id, p_status, v_note);
  PERFORM set_config('jobsearch.source', '', true);
  RETURN v_result;
END;
$$;

-- ---------------------------------------------------------------- 6. the dashboard's role
REVOKE ALL ON FUNCTION dashboard_job_events(bigint), dashboard_board(), dashboard_stats() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION dashboard_job_events(bigint), dashboard_board(), dashboard_stats() TO jobsearch_dashboard;
