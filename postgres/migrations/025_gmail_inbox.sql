-- 025: Gmail status tracking, step 6f (docs/gmail.md): the dashboard's email inbox.
--
-- The dashboard role has no access to the Gmail tables. These SECURITY DEFINER functions are the whole of what it may do with
-- suggestions, like every other panel:
--
--  * dashboard_email_inbox(state, limit)   the suggestions in one state (pending, accepted, dismissed), newest first, with what the
--                                          page needs to decide: the job, the email's sender domain, subject and short excerpt, what it
--                                          would change, how sure, the flags, whether it is STALE (the job has moved on so the change
--                                          no longer makes sense), whether it needs an explicit acknowledgement (a trust flag or low
--                                          confidence), whether it can be accepted, and whether an accepted one can still be undone.
--  * dashboard_email_count()               how many wait, and whether the Gmail part is on (for the menu's badge).
--  * dashboard_email_accept(id, expected, ack)   the change, made by dashboard_move_status(): the SAME function as a click on the
--                                          board, so it is recorded in the job's history and Undo works. Refused when the suggestion
--                                          was already decided (JD409), when it is stale (JD409, the current status in DETAIL), when
--                                          it needs an acknowledgement that was not given (JD422), or when the job is scam-blocked
--                                          (JD403, by dashboard_set_status). Other pending suggestions for the job are left alone: whether one
--                                          still fits is worked out each time it is read (stale), so an Undo never strands them.
--  * dashboard_email_dismiss(id) / dashboard_email_restore(id)   close a suggestion, or bring one back: a dismissed one, or an accepted
--                                          one whose change was undone or overtaken (the job no longer has the status it proposed).
--  * email_transition_ok(signal, status)   the rule of scraper/mail_rules.propose() ("propose" outcomes only): what a signal may change.
--  * dashboard_health() (replaced)         two more alerts, only while the Gmail part is on: the login expired, and the reader keeps
--                                          ending early.
-- Nothing here reads a mailbox, and no function takes text from the page that reaches an email table (an id and two flags).

-- What a signal may change from a given application status ('none' when there is no application). The same table as
-- scraper/mail_rules.propose(): a test compares the two for every pair.
CREATE FUNCTION email_transition_ok(p_signal text, p_status text) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE p_signal
    WHEN 'acknowledgement' THEN coalesce(p_status, 'none') IN ('none', 'drafted', 'approved')
    WHEN 'interview'       THEN coalesce(p_status, 'none') IN ('none', 'drafted', 'approved', 'applied', 'in_progress')
    WHEN 'assessment'      THEN coalesce(p_status, 'none') IN ('none', 'drafted', 'approved', 'applied', 'in_progress')
    WHEN 'offer'           THEN coalesce(p_status, 'none') NOT IN ('offer', 'rejected', 'withdrawn')
    WHEN 'rejection'       THEN coalesce(p_status, 'none') NOT IN ('rejected', 'withdrawn', 'offer')
    ELSE false END
$$;

-- The flags and the confidence that make Accept ask for a second look.
CREATE FUNCTION email_needs_ack(p_flags text[], p_confidence text) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
  SELECT p_confidence = 'low'
      OR coalesce(p_flags, '{}') && ARRAY['conflict', 'phishing_risk', 'markup_doubt', 'free_mail_sender', 'suspicious_link', 'scam_pattern',
                                          'injection_attempt', 'sender_domain_not_the_company', 'unverified_sender', 'display_name_only',
                                          'unfamiliar_domain_ending', 'domain_matches_first_word_only']
$$;

CREATE FUNCTION dashboard_email_inbox(p_state text DEFAULT 'pending', p_limit int DEFAULT 50) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v_items jsonb;
BEGIN
  IF p_state IS NULL OR p_state NOT IN ('pending', 'accepted', 'dismissed') THEN
    RAISE EXCEPTION 'state must be pending, accepted or dismissed' USING ERRCODE = 'JD422';
  END IF;
  SELECT coalesce(jsonb_agg(to_jsonb(x) ORDER BY x.sort_at DESC, x.id DESC), '[]'::jsonb) INTO v_items FROM (
    SELECT s.id, s.job_id, j.company, j.title, j.status AS job_status, coalesce(a.status, j.status) AS current_status,
           s.received_at, s.from_domain, s.subject, s.excerpt, s.signal, s.proposed_status, s.status_at_proposal, s.confidence, s.source,
           s.flags, s.state, s.created_at, s.decided_at, s.event_id,
           coalesce(s.decided_at, s.created_at) AS sort_at,
           (s.state = 'pending' AND coalesce(a.status, 'none') IS DISTINCT FROM s.status_at_proposal
              AND NOT email_transition_ok(s.signal, coalesce(a.status, 'none'))) AS stale,
           email_needs_ack(s.flags, s.confidence) AS needs_ack,
           (s.state = 'pending' AND j.status <> 'scam_blocked'
              AND (coalesce(a.status, 'none') IS NOT DISTINCT FROM s.status_at_proposal OR email_transition_ok(s.signal, coalesce(a.status, 'none')))) AS can_accept,
           (s.state = 'accepted' AND s.event_id IS NOT NULL AND entry_is_undoable(s.event_id)) AS can_undo,
           (s.state = 'dismissed' OR (s.state = 'accepted' AND coalesce(a.status, 'none') IS DISTINCT FROM s.proposed_status)) AS can_restore
      FROM email_suggestions s
      JOIN jobs j ON j.id = s.job_id
      LEFT JOIN applications a ON a.job_id = s.job_id
     WHERE s.state = p_state
     ORDER BY coalesce(s.decided_at, s.created_at) DESC, s.id DESC
     LIMIT greatest(1, least(coalesce(p_limit, 50), 100))) x;
  RETURN jsonb_build_object(
    'enabled',  coalesce((SELECT gmail FROM feature_settings WHERE id = 1), false),
    'login',    coalesce((SELECT token_status FROM gmail_state WHERE id = 1), 'none'),
    'counts',   jsonb_build_object('pending',   (SELECT count(*) FROM email_suggestions WHERE state = 'pending'),
                                   'accepted',  (SELECT count(*) FROM email_suggestions WHERE state = 'accepted'),
                                   'dismissed', (SELECT count(*) FROM email_suggestions WHERE state = 'dismissed')),
    'items',    v_items);
END;
$$;

CREATE FUNCTION dashboard_email_count() RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT jsonb_build_object('enabled', coalesce((SELECT gmail FROM feature_settings WHERE id = 1), false),
                            'pending', (SELECT count(*) FROM email_suggestions WHERE state = 'pending'))
$$;

CREATE FUNCTION dashboard_email_accept(p_id bigint, p_expected text, p_ack boolean DEFAULT false) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE s record; v_app text; v_move jsonb;
BEGIN
  SELECT * INTO s FROM email_suggestions WHERE id = p_id FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'no suggestion %', p_id USING ERRCODE = 'JD404';
  END IF;
  IF s.state <> 'pending' THEN
    RAISE EXCEPTION 'suggestion % was already %', p_id, s.state USING ERRCODE = 'JD409';
  END IF;
  SELECT status INTO v_app FROM applications WHERE job_id = s.job_id;
  IF coalesce(v_app, 'none') IS DISTINCT FROM s.status_at_proposal AND NOT email_transition_ok(s.signal, coalesce(v_app, 'none')) THEN
    RAISE EXCEPTION 'the job changed since this email was read' USING ERRCODE = 'JD409',
      DETAIL = coalesce(v_app, (SELECT status FROM jobs WHERE id = s.job_id));
  END IF;
  IF email_needs_ack(s.flags, s.confidence) AND NOT coalesce(p_ack, false) THEN
    RAISE EXCEPTION 'this suggestion needs a second look: check the sender, then accept it again' USING ERRCODE = 'JD422';
  END IF;
  -- the same function as a click on the board: the history entry, the checks, the Undo
  v_move := dashboard_move_status(s.job_id, s.proposed_status, p_expected, 'accepted from an email suggestion');
  UPDATE email_suggestions SET state = 'accepted', decided_at = now(), event_id = (v_move->>'event_id')::bigint WHERE id = p_id;
  RETURN jsonb_build_object('id', p_id, 'job_id', s.job_id, 'status', v_move->>'status', 'event_id', v_move->'event_id');
END;
$$;

CREATE FUNCTION dashboard_email_dismiss(p_id bigint) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v_state text;
BEGIN
  SELECT state INTO v_state FROM email_suggestions WHERE id = p_id FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'no suggestion %', p_id USING ERRCODE = 'JD404';
  END IF;
  IF v_state <> 'pending' THEN
    RAISE EXCEPTION 'suggestion % was already %', p_id, v_state USING ERRCODE = 'JD409';
  END IF;
  UPDATE email_suggestions SET state = 'dismissed', decided_at = now() WHERE id = p_id;
  RETURN jsonb_build_object('id', p_id, 'state', 'dismissed');
END;
$$;

CREATE FUNCTION dashboard_email_restore(p_id bigint) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE s record; v_app text;
BEGIN
  SELECT * INTO s FROM email_suggestions WHERE id = p_id FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'no suggestion %', p_id USING ERRCODE = 'JD404';
  END IF;
  SELECT status INTO v_app FROM applications WHERE job_id = s.job_id;
  IF NOT (s.state = 'dismissed' OR (s.state = 'accepted' AND coalesce(v_app, 'none') IS DISTINCT FROM s.proposed_status)) THEN
    RAISE EXCEPTION 'only a dismissed suggestion, or an accepted one the job no longer reflects, can be brought back; this one is %', s.state USING ERRCODE = 'JD409';
  END IF;
  BEGIN
    UPDATE email_suggestions SET state = 'pending', decided_at = NULL, event_id = NULL WHERE id = p_id;
  EXCEPTION WHEN unique_violation THEN
    RAISE EXCEPTION 'another suggestion for this job and status is already waiting' USING ERRCODE = 'JD409';
  END;
  RETURN jsonb_build_object('id', p_id, 'state', 'pending');
END;
$$;

CREATE OR REPLACE FUNCTION dashboard_health() RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  WITH ctl AS (
    SELECT paused, paused_at, reason FROM schedule_control WHERE id = 1
  ), tz AS (
    SELECT timezone AS z FROM budget_settings WHERE id = 1
  ), feat AS (
    SELECT tailoring, telegram, digest, gmail FROM feature_settings WHERE id = 1
  ), sch AS (
    SELECT s.name, s.enabled, s.every_minutes, s.at_time, s.batch, s.model, s.last_run_at, s.next_run_at,
           -- the scheduler leaves these alone while their switch is off (digest also needs Telegram on; a
           -- missing bot token or Google login in .env is not visible from here)
           coalesce((s.name = 'tailor' AND NOT f.tailoring) OR (s.name = 'digest' AND NOT (f.digest AND f.telegram))
                    OR (s.name = 'mail' AND NOT f.gmail), false) AS feature_off,
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
    UNION ALL
    SELECT 9, 'bad', 'gmail_login',
           'The Gmail login expired or was revoked, so no replies are being read. Sign in again with scripts/jobsearch gmail login.'
      FROM feat f, gmail_state g WHERE f.gmail AND g.id = 1 AND g.token_status = 'expired'
    UNION ALL
    SELECT 10, 'warn', 'gmail_stalled',
           'The mail reader ended early ' || g.early_stops || ' runs in a row (Gmail limiting requests, Claude not answering, or a deadline). See scripts/jobsearch gmail.'
      FROM feat f, gmail_state g WHERE f.gmail AND g.id = 1 AND g.early_stops >= 3
  )
  SELECT jsonb_build_object(
    'generated_at', now(),
    'paused', (SELECT jsonb_build_object('paused', paused, 'paused_at', paused_at, 'reason', reason) FROM ctl),
    'timezone', (SELECT timezone FROM budget_settings WHERE id = 1),
    'schedules', coalesce((SELECT jsonb_agg(jsonb_build_object(
        'name', name, 'enabled', enabled, 'every_minutes', every_minutes, 'at_time', at_time, 'batch', batch,
        'model', model, 'last_run_at', last_run_at, 'next_run_at', next_run_at, 'overdue', overdue,
        'feature_off', feature_off)
        ORDER BY CASE name WHEN 'collect' THEN 1 WHEN 'score' THEN 2 WHEN 'tailor' THEN 3 WHEN 'digest' THEN 4 ELSE 5 END, name) FROM sch), '[]'),
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

REVOKE ALL ON FUNCTION email_transition_ok(text, text), email_needs_ack(text[], text), dashboard_email_inbox(text, int), dashboard_email_count(),
                       dashboard_email_accept(bigint, text, boolean), dashboard_email_dismiss(bigint), dashboard_email_restore(bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION dashboard_email_inbox(text, int), dashboard_email_count(), dashboard_email_accept(bigint, text, boolean),
                          dashboard_email_dismiss(bigint), dashboard_email_restore(bigint) TO jobsearch_dashboard;
