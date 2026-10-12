-- 016: the web dashboard, version 2a: the health panel, the spend panel and the pause switch (docs/dashboard.md).
--
--  1. dashboard_health(): one jsonb for the health panel: the pause state, every schedule's last and next run, the
--     scoring queue, parked jobs, boards and feeds that keep failing, and a list of alerts computed from them.
--  2. dashboard_spend(): one jsonb for the spend panel: Claude and Apify spend against their caps, a daily history
--     by purpose, and this month by model.
--  3. dashboard_set_pause(): the same one-row update as the scraper's set_schedule_paused(), so the dashboard's
--     pause button and `scripts/jobsearch schedule pause` act on the same switch.
--  The functions are SECURITY DEFINER and granted to jobsearch_dashboard only, like those of migration 014: the role
--  still has no table privileges. Nothing existing is changed.
--
-- Rules behind the alerts (the doctor's, where the doctor has one):
--   paused                  the global pause is on
--   claude_blocked / warn   budget_status(): a cap reached, or warn_pct of the monthly cap spent
--   apify_blocked           the Apify MONTHLY cap reached (Apify stops, the rest keeps running). The daily cap is
--                           about a thirtieth of it and is routinely reached on a collect day: the meter shows it,
--                           an alert for it would be noise
--   scoring_stalled         10 or more jobs waiting over 3 hours and nothing scored for 3 hours (the doctor's rule);
--                           jobs that ran out of attempts do not count as waiting, they are their own alert
--   parked                  jobs that ran out of attempts (v_parked_jobs)
--   sources_failing         a board or feed that failed 3 times in a row
--   schedule_overdue        collect or score is more than 30 minutes past its next run while nothing is paused,
--                           which means the scheduler workflow is not ticking (it ticks every 5 minutes). Only those
--                           two: the scheduler deliberately skips tailor and digest while their feature is off
--                           (scraper/app.py schedule_tick), so their last run goes stale on purpose

-- ---------------------------------------------------------------- 1. health
CREATE FUNCTION dashboard_health() RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  WITH ctl AS (
    SELECT paused, paused_at, reason FROM schedule_control WHERE id = 1
  ), tz AS (
    SELECT timezone AS z FROM budget_settings WHERE id = 1
  ), feat AS (
    SELECT tailoring, telegram, digest FROM feature_settings WHERE id = 1
  ), sch AS (
    SELECT s.name, s.enabled, s.every_minutes, s.at_time, s.batch, s.model, s.last_run_at, s.next_run_at,
           -- the scheduler leaves these two alone while their switch is off (digest also needs Telegram on; a
           -- missing bot token in .env is not visible from here)
           coalesce((s.name = 'tailor' AND NOT f.tailoring) OR (s.name = 'digest' AND NOT (f.digest AND f.telegram)), false) AS feature_off,
           (s.enabled AND NOT (SELECT paused FROM ctl) AND s.name IN ('collect', 'score')
            AND ( s.next_run_at < now() - interval '30 minutes'
               -- never ran: due at the next tick, so measure from when the schedule was last edited
               OR (s.last_run_at IS NULL AND s.every_minutes IS NOT NULL AND ss.updated_at < now() - interval '30 minutes'))) AS overdue
      FROM schedule_status() s
      JOIN schedule_settings ss ON ss.name = s.name
      LEFT JOIN feat f ON true
  ), parked AS (
    SELECT stage, job_id, title, company, claims, strikes FROM v_parked_jobs
  ), q AS (
    SELECT count(*) FILTER (WHERE status = 'new')                                              AS waiting,
           count(*) FILTER (WHERE status = 'new' AND first_seen < now() - interval '3 hours')  AS waiting_over_3h,
           max(scored_at)                                                                      AS last_scored_at
      FROM jobs
     WHERE id NOT IN (SELECT job_id FROM parked WHERE stage = 'score')
  ), src AS (
    SELECT source, key, last_fetched_at, last_ok_at, last_count, failures, last_error FROM fetch_state
  ), cs AS (
    SELECT * FROM budget_status()
  ), ap AS (
    SELECT * FROM apify_budget_status()
  ), alerts(ord, level, code, message) AS (
    SELECT 1, 'bad', 'paused',
           'Everything is paused' || coalesce(': ' || ctl.reason, '') || '. Nothing scheduled runs until you resume.'
      FROM ctl WHERE ctl.paused
    UNION ALL
    SELECT 2, 'bad', 'claude_blocked', 'Claude spending is stopped: ' || cs.blocked_reason || '.' FROM cs WHERE cs.blocked
    UNION ALL
    SELECT 3, 'warn', 'claude_warn',
           'Claude spend is at ' || round(100 * cs.month_spent / nullif(cs.monthly_cap, 0)) || '% of the monthly cap.'
      FROM cs WHERE cs.warn AND NOT cs.blocked
    UNION ALL
    SELECT 4, 'warn', 'apify_blocked', 'Apify is stopped: ' || ap.blocked_reason || '. Other sources keep running.'
      FROM ap WHERE ap.blocked_reason = 'Apify monthly cap reached'
    UNION ALL
    SELECT 5, 'warn', 'scoring_stalled',
           q.waiting_over_3h || ' jobs have waited over 3 hours and nothing was scored for ' ||
           CASE WHEN q.last_scored_at IS NULL THEN 'a long time'
                ELSE round(extract(epoch FROM now() - q.last_scored_at) / 3600) || ' h' END || '.'
      FROM q WHERE q.waiting_over_3h >= 10 AND (q.last_scored_at IS NULL OR q.last_scored_at < now() - interval '3 hours')
    UNION ALL
    SELECT 6, 'warn', 'parked', (SELECT count(*) FROM parked) || ' job(s) ran out of attempts and are skipped until requeued.'
     WHERE EXISTS (SELECT 1 FROM parked)
    UNION ALL
    SELECT 7, 'warn', 'sources_failing',
           (SELECT count(*) FROM src WHERE failures >= 3) || ' board or feed source(s) failed 3 times in a row.'
     WHERE EXISTS (SELECT 1 FROM src WHERE failures >= 3)
    UNION ALL
    SELECT 8, 'warn', 'schedule_overdue',
           'The ' || sch.name || ' schedule is overdue (it was due at ' ||
           to_char(sch.next_run_at AT TIME ZONE (SELECT z FROM tz), 'YYYY-MM-DD HH24:MI') || ' ' || (SELECT z FROM tz) ||
           '). The scheduler workflow may not be running.'
      FROM sch WHERE sch.overdue
  )
  SELECT jsonb_build_object(
    'generated_at', now(),
    'paused', (SELECT jsonb_build_object('paused', paused, 'paused_at', paused_at, 'reason', reason) FROM ctl),
    'timezone', (SELECT timezone FROM budget_settings WHERE id = 1),
    'schedules', coalesce((SELECT jsonb_agg(jsonb_build_object(
        'name', name, 'enabled', enabled, 'every_minutes', every_minutes, 'at_time', at_time, 'batch', batch,
        'model', model, 'last_run_at', last_run_at, 'next_run_at', next_run_at, 'overdue', overdue,
        'feature_off', feature_off)
        ORDER BY CASE name WHEN 'collect' THEN 1 WHEN 'score' THEN 2 WHEN 'tailor' THEN 3 ELSE 4 END, name) FROM sch), '[]'),
    'queue', (SELECT jsonb_build_object('waiting_to_score', waiting, 'waiting_over_3h', waiting_over_3h,
                                        'last_scored_at', last_scored_at) FROM q),
    'parked', jsonb_build_object(
        'total', (SELECT count(*) FROM parked),
        'items', coalesce((SELECT jsonb_agg(to_jsonb(p)) FROM (SELECT * FROM parked ORDER BY stage, job_id LIMIT 20) p), '[]')),
    'sources', jsonb_build_object(
        'tracked', (SELECT count(*) FROM src),
        'failing', (SELECT count(*) FROM src WHERE failures > 0),
        'items', coalesce((SELECT jsonb_agg(to_jsonb(f)) FROM (
            SELECT * FROM src WHERE failures > 0 ORDER BY failures DESC, source, key LIMIT 50) f), '[]')),
    'alerts', coalesce((SELECT jsonb_agg(jsonb_build_object('level', level, 'code', code, 'message', message) ORDER BY ord)
                          FROM alerts), '[]'))
$$;

-- ---------------------------------------------------------------- 2. spend
-- The history is the last 30 days (in the configured timezone), not the calendar month, so the first days of a month
-- do not show an empty chart. Every call is counted in `calls`, including free (refunded) ones and failed ones.
CREATE FUNCTION dashboard_spend() RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  WITH tz AS (
    SELECT timezone AS z FROM budget_settings WHERE id = 1
  ), daily AS (
    SELECT (u.at AT TIME ZONE tz.z)::date AS day, u.provider, u.purpose, count(*) AS calls,
           round(sum(u.cost_usd), 4) AS cost_usd
      FROM llm_usage u, tz
     WHERE u.at >= ((now() AT TIME ZONE tz.z)::date - 29) AT TIME ZONE tz.z
     GROUP BY 1, 2, 3
  ), models AS (
    SELECT u.provider, coalesce(u.model, '(none)') AS model, count(*) AS calls, round(sum(u.cost_usd), 4) AS cost_usd,
           coalesce(sum(u.input_tokens), 0) AS input_tokens, coalesce(sum(u.output_tokens), 0) AS output_tokens
      FROM llm_usage u, budget_status() b
     WHERE u.at >= b.month_start
     GROUP BY 1, 2
  )
  SELECT jsonb_build_object(
    'generated_at', now(),
    'timezone', (SELECT z FROM tz),
    'claude', (SELECT to_jsonb(b) FROM budget_status() b),
    'apify', (SELECT to_jsonb(a) FROM apify_budget_status() a),
    'daily', coalesce((SELECT jsonb_agg(to_jsonb(d) ORDER BY d.day, d.provider, d.purpose) FROM daily d), '[]'),
    'models', coalesce((SELECT jsonb_agg(to_jsonb(m) ORDER BY m.cost_usd DESC, m.model) FROM models m), '[]'))
$$;

-- ---------------------------------------------------------------- 3. pause and resume
-- Exactly what the scraper's set_schedule_paused() does (scraper/db.py): the reason is cut to 200 characters and
-- a blank one is none, pausing stamps the time, resuming clears both. Returns the new state.
CREATE FUNCTION dashboard_set_pause(p_paused boolean, p_reason text DEFAULT NULL) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v_reason text := nullif(btrim(left(p_reason, 200)), '');
BEGIN
  IF p_paused IS NULL THEN
    RAISE EXCEPTION 'paused must be true or false' USING ERRCODE = 'JD422';
  END IF;
  UPDATE schedule_control
     SET paused = p_paused,
         paused_at = CASE WHEN p_paused THEN now() END,
         reason = CASE WHEN p_paused THEN v_reason END
   WHERE id = 1;
  RETURN (SELECT jsonb_build_object('paused', paused, 'paused_at', paused_at, 'reason', reason)
            FROM schedule_control WHERE id = 1);
END;
$$;

-- ---------------------------------------------------------------- 4. the dashboard role
REVOKE ALL ON FUNCTION dashboard_health(), dashboard_spend(), dashboard_set_pause(boolean, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION dashboard_health(), dashboard_spend(), dashboard_set_pause(boolean, text) TO jobsearch_dashboard;
