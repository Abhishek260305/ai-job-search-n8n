-- 019: notes, contacts and follow-up reminders (docs/dashboard.md, version 3b).
--
--  1. job_tracking: your own notes about a job, who you talk to there, who referred you and a date to follow up on.
--     One row per job (any job: you may note a recruiter before you apply), removed again when every field is empty.
--  2. follow_up_settings: after how many days an application with no reply is worth a nudge (7), and whether the
--     daily Telegram message mentions what is due (off: nothing changes for anyone who does not ask for it).
--  3. follow_ups_due(): what is due today. The rule, in order:
--       - a follow-up date you set that is today or in the past: due ("date");
--       - a follow-up date in the future: not due, whatever else is true (you decided when);
--       - otherwise an application still at 'applied' for at least the set number of days: due ("waiting").
--     Rejected, withdrawn, skipped and scam-blocked jobs are never due. "Today" is in the configured time zone.
--  4. The dashboard's functions: dashboard_job_tracking(), dashboard_set_tracking() (the change only goes through if the
--     row is still as the browser last saw it, like the search profile), dashboard_set_follow_up_days(); and
--     dashboard_board() now also lists what is due and flags the cards.
--  Same as before: SECURITY DEFINER, granted to jobsearch_dashboard only, no table privileges.
--
-- SQLSTATEs: JD404 no such job, JD412 the tracking changed elsewhere, JD422 bad input.

-- ---------------------------------------------------------------- 1. the table
CREATE TABLE job_tracking (
  job_id        bigint PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
  notes         text CHECK (length(notes) <= 2000),
  contact_name  text CHECK (length(contact_name) <= 100),
  contact_email text CHECK (length(contact_email) <= 200 AND contact_email ~ '^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$'),
  referral      text CHECK (length(referral) <= 200),
  follow_up_on  date CHECK (follow_up_on BETWEEN DATE '2000-01-01' AND DATE '2100-01-01'),
  updated_at    timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- ---------------------------------------------------------------- 2. the settings
CREATE TABLE follow_up_settings (
  id         int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  days       int NOT NULL DEFAULT 7 CHECK (days BETWEEN 1 AND 90),
  digest     boolean NOT NULL DEFAULT false,
  updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO follow_up_settings DEFAULT VALUES;

-- ---------------------------------------------------------------- 3. what is due
CREATE FUNCTION follow_ups_due() RETURNS TABLE (job_id bigint, reason text, follow_up_on date, applied_on date, days_waiting int)
LANGUAGE sql STABLE AS $$
  WITH tz AS (
    SELECT timezone AS z FROM budget_settings WHERE id = 1
  ), t AS (
    SELECT (now() AT TIME ZONE z)::date AS today, z FROM tz
  ), base AS (
    SELECT k.job_id, jt.follow_up_on, a.status AS app_status, (a.applied_at AT TIME ZONE t.z)::date AS applied_on
      FROM (SELECT x.job_id FROM applications x UNION SELECT y.job_id FROM job_tracking y) k
      JOIN jobs j ON j.id = k.job_id
      LEFT JOIN job_tracking jt ON jt.job_id = k.job_id
      LEFT JOIN applications a ON a.job_id = k.job_id
      CROSS JOIN t
     WHERE j.status <> 'scam_blocked'
       AND coalesce(a.status, j.status) NOT IN ('rejected', 'withdrawn', 'skipped', 'expired', 'filtered_out')
  )
  SELECT b.job_id,
         CASE WHEN b.follow_up_on IS NOT NULL THEN 'date' ELSE 'waiting' END,
         b.follow_up_on, b.applied_on,
         CASE WHEN b.applied_on IS NOT NULL THEN t.today - b.applied_on END
    FROM base b CROSS JOIN t CROSS JOIN follow_up_settings s
   WHERE s.id = 1
     AND ((b.follow_up_on IS NOT NULL AND b.follow_up_on <= t.today)
          OR (b.follow_up_on IS NULL AND b.app_status = 'applied' AND b.applied_on IS NOT NULL
              AND t.today - b.applied_on >= s.days))
$$;

-- ---------------------------------------------------------------- 4. the dashboard's functions
CREATE FUNCTION tracking_today() RETURNS date LANGUAGE sql STABLE AS $$
  SELECT (now() AT TIME ZONE coalesce((SELECT timezone FROM budget_settings WHERE id = 1), 'UTC'))::date
$$;

CREATE FUNCTION tracking_version(p timestamptz) RETURNS text LANGUAGE sql IMMUTABLE AS $$
  SELECT to_char(p AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
$$;

CREATE FUNCTION dashboard_job_tracking(p_job_id bigint) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM jobs WHERE id = p_job_id) THEN
    RAISE EXCEPTION 'no job %', p_job_id USING ERRCODE = 'JD404';
  END IF;
  RETURN coalesce((SELECT jsonb_build_object('notes', t.notes, 'contact_name', t.contact_name,
                                             'contact_email', t.contact_email, 'referral', t.referral,
                                             'follow_up_on', t.follow_up_on, 'version', tracking_version(t.updated_at),
                                             'today', tracking_today())
                     FROM job_tracking t WHERE t.job_id = p_job_id),
                  jsonb_build_object('notes', NULL, 'contact_name', NULL, 'contact_email', NULL, 'referral', NULL,
                                     'follow_up_on', NULL, 'version', NULL, 'today', tracking_today()));
END;
$$;

-- p_patch holds only the fields to change; null or an empty string clears one. p_expected is the version the browser
-- read (null: there was no row yet): a different one means the tracking was changed since, and nothing is written.
CREATE FUNCTION dashboard_set_tracking(p_job_id bigint, p_expected text, p_patch jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
  v_key text; v_val jsonb; v_text text; v_cur job_tracking; v_found boolean;
  v_notes text; v_name text; v_email text; v_ref text; v_date date;
BEGIN
  IF p_patch IS NULL OR jsonb_typeof(p_patch) <> 'object' OR p_patch = '{}'::jsonb THEN
    RAISE EXCEPTION 'nothing to change' USING ERRCODE = 'JD422';
  END IF;
  FOR v_key, v_val IN SELECT * FROM jsonb_each(p_patch) LOOP
    IF v_key NOT IN ('notes', 'contact_name', 'contact_email', 'referral', 'follow_up_on') THEN
      RAISE EXCEPTION 'unknown field %', left(v_key, 40) USING ERRCODE = 'JD422';
    END IF;
    IF jsonb_typeof(v_val) NOT IN ('string', 'null') THEN
      RAISE EXCEPTION '% must be text', v_key USING ERRCODE = 'JD422';
    END IF;
  END LOOP;

  PERFORM 1 FROM jobs WHERE id = p_job_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'no job %', p_job_id USING ERRCODE = 'JD404'; END IF;

  SELECT * INTO v_cur FROM job_tracking WHERE job_id = p_job_id;
  v_found := FOUND;
  IF (CASE WHEN v_found THEN tracking_version(v_cur.updated_at) END) IS DISTINCT FROM p_expected THEN
    RAISE EXCEPTION 'tracking of job % changed', p_job_id USING ERRCODE = 'JD412';
  END IF;

  v_notes := v_cur.notes; v_name := v_cur.contact_name; v_email := v_cur.contact_email;
  v_ref := v_cur.referral; v_date := v_cur.follow_up_on;
  IF p_patch ? 'notes' THEN
    v_notes := nullif(btrim(p_patch->>'notes', E' \t\r\n'), '');
    IF length(v_notes) > 2000 THEN RAISE EXCEPTION 'notes are longer than 2000 characters' USING ERRCODE = 'JD422'; END IF;
  END IF;
  IF p_patch ? 'contact_name' THEN
    v_name := nullif(btrim(p_patch->>'contact_name', E' \t\r\n'), '');
    IF length(v_name) > 100 THEN RAISE EXCEPTION 'the contact name is longer than 100 characters' USING ERRCODE = 'JD422'; END IF;
  END IF;
  IF p_patch ? 'contact_email' THEN
    v_email := nullif(btrim(p_patch->>'contact_email', E' \t\r\n'), '');
    IF v_email IS NOT NULL AND (length(v_email) > 200 OR v_email !~ '^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$') THEN
      RAISE EXCEPTION 'that does not look like an email address' USING ERRCODE = 'JD422';
    END IF;
  END IF;
  IF p_patch ? 'referral' THEN
    v_ref := nullif(btrim(p_patch->>'referral', E' \t\r\n'), '');
    IF length(v_ref) > 200 THEN RAISE EXCEPTION 'the referral is longer than 200 characters' USING ERRCODE = 'JD422'; END IF;
  END IF;
  IF p_patch ? 'follow_up_on' THEN
    v_text := nullif(btrim(p_patch->>'follow_up_on', E' \t\r\n'), '');
    IF v_text IS NULL THEN
      v_date := NULL;
    ELSE
      IF v_text !~ '^\d{4}-\d{2}-\d{2}$' THEN RAISE EXCEPTION 'the follow-up date must look like 2026-10-31' USING ERRCODE = 'JD422'; END IF;
      BEGIN v_date := v_text::date;
      EXCEPTION WHEN OTHERS THEN RAISE EXCEPTION 'that is not a real date' USING ERRCODE = 'JD422'; END;
      IF v_date < DATE '2000-01-01' OR v_date > tracking_today() + 730 THEN
        RAISE EXCEPTION 'the follow-up date must be within the next two years' USING ERRCODE = 'JD422';
      END IF;
    END IF;
  END IF;

  IF v_notes IS NULL AND v_name IS NULL AND v_email IS NULL AND v_ref IS NULL AND v_date IS NULL THEN
    DELETE FROM job_tracking WHERE job_id = p_job_id;
    RETURN jsonb_build_object('notes', NULL, 'contact_name', NULL, 'contact_email', NULL, 'referral', NULL,
                              'follow_up_on', NULL, 'version', NULL, 'today', tracking_today());
  END IF;
  -- nothing actually changed: no new version, so another tab is not told of a change that never happened
  IF v_found AND v_notes IS NOT DISTINCT FROM v_cur.notes AND v_name IS NOT DISTINCT FROM v_cur.contact_name
     AND v_email IS NOT DISTINCT FROM v_cur.contact_email AND v_ref IS NOT DISTINCT FROM v_cur.referral
     AND v_date IS NOT DISTINCT FROM v_cur.follow_up_on THEN
    RETURN dashboard_job_tracking(p_job_id);
  END IF;
  INSERT INTO job_tracking (job_id, notes, contact_name, contact_email, referral, follow_up_on, updated_at)
  VALUES (p_job_id, v_notes, v_name, v_email, v_ref, v_date, clock_timestamp())
  ON CONFLICT (job_id) DO UPDATE SET notes = EXCLUDED.notes, contact_name = EXCLUDED.contact_name,
         contact_email = EXCLUDED.contact_email, referral = EXCLUDED.referral, follow_up_on = EXCLUDED.follow_up_on,
         updated_at = EXCLUDED.updated_at;
  RETURN dashboard_job_tracking(p_job_id);
END;
$$;

CREATE FUNCTION dashboard_set_follow_up_days(p_days int) RETURNS int
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
  IF p_days IS NULL OR p_days < 1 OR p_days > 90 THEN
    RAISE EXCEPTION 'days must be between 1 and 90' USING ERRCODE = 'JD422';
  END IF;
  UPDATE follow_up_settings SET days = p_days, updated_at = now() WHERE id = 1;
  RETURN p_days;
END;
$$;

-- the board, with what is due: each card says why it is due (or null), and the list of what is due comes with it
CREATE OR REPLACE FUNCTION dashboard_board() RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  WITH due AS (
    SELECT * FROM follow_ups_due()
  ), cards AS (
    SELECT v.id, v.title, v.company, v.location, v.is_remote, v.score, v.pipeline_status,
           CASE WHEN v.pipeline_status IN ('shortlisted', 'tailored', 'drafted', 'approved') THEN 'not_applied'
                WHEN v.pipeline_status IN ('applied', 'in_progress', 'interview', 'offer', 'rejected') THEN v.pipeline_status
           END AS col,
           v.applied_at, v.deadline, v.closing_soon, v.has_resume, v.has_cover_letter, v.needs_attention,
           d.reason AS follow_up,
           -- not applied yet: when it was scored. Otherwise: the last real change of status (a note does not count)
           CASE WHEN v.pipeline_status IN ('shortlisted', 'tailored', 'drafted', 'approved')
                THEN coalesce(j.scored_at, v.first_seen)
                ELSE coalesce((SELECT max(e.at) FROM application_events e
                                WHERE e.job_id = v.id AND e.from_status IS DISTINCT FROM e.to_status),
                              v.applied_at, v.first_seen) END AS status_since
      FROM v_dashboard_jobs v
      JOIN jobs j ON j.id = v.id
      LEFT JOIN due d ON d.job_id = v.id
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
              'has_cover_letter', r.has_cover_letter, 'needs_attention', r.needs_attention, 'follow_up', r.follow_up,
              'status_since', r.status_since) ORDER BY r.rn)
            FROM ranked r WHERE r.col = cols.key AND r.rn <= 40), '[]'))
        ORDER BY cols.ord) FROM cols),
    'today', tracking_today(),
    'follow_up_days', (SELECT days FROM follow_up_settings WHERE id = 1),
    'follow_ups_total', (SELECT count(*) FROM due),
    'follow_ups', coalesce((SELECT jsonb_agg(jsonb_build_object(
          'id', j.id, 'title', j.title, 'company', j.company, 'location', j.location, 'score', j.score,
          'pipeline_status', coalesce(a.status, j.status), 'reason', d.reason, 'follow_up_on', d.follow_up_on,
          'applied_on', d.applied_on, 'days_waiting', d.days_waiting,
          'contact_name', t.contact_name, 'contact_email', t.contact_email, 'referral', t.referral)
          ORDER BY (d.reason = 'date') DESC, d.follow_up_on, d.applied_on, j.id)
        FROM (SELECT * FROM due ORDER BY (reason = 'date') DESC, follow_up_on, applied_on, job_id LIMIT 20) d
        JOIN jobs j ON j.id = d.job_id
        LEFT JOIN applications a ON a.job_id = d.job_id
        LEFT JOIN job_tracking t ON t.job_id = d.job_id), '[]'),
    'closed', jsonb_build_object(
        'withdrawn', (SELECT count(*) FROM cards WHERE pipeline_status = 'withdrawn'),
        'skipped',   (SELECT count(*) FROM cards WHERE pipeline_status = 'skipped')))
$$;

-- ---------------------------------------------------------------- 5. the dashboard's role
-- the helpers are for the functions above (and the scraper and CLI, which are the owner): not for the dashboard role
REVOKE ALL ON FUNCTION follow_ups_due(), tracking_version(timestamptz), tracking_today() FROM PUBLIC;
REVOKE ALL ON FUNCTION dashboard_job_tracking(bigint), dashboard_set_tracking(bigint, text, jsonb),
                       dashboard_set_follow_up_days(int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION dashboard_job_tracking(bigint), dashboard_set_tracking(bigint, text, jsonb),
                          dashboard_set_follow_up_days(int) TO jobsearch_dashboard;
