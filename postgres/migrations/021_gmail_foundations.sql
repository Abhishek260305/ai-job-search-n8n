-- 021: Gmail status tracking, foundations (Phase 6a, docs/gmail.md).
--
-- Nothing here reads a mailbox. It lays the ground the later steps stand on, and everything is off until you turn it on:
--
--  * feature_settings.gmail   the switch for the optional Gmail part. OFF by default (unlike the other three parts):
--                             a part that needs a Google login must be asked for.
--  * a `mail` schedule        one more stage next to collect, score, tailor and digest: every 30 minutes by default,
--                             `batch` is how many messages one run may open. The scheduler does not claim it while the
--                             Gmail part is off, so it starts promptly when you switch it on.
--  * gmail_state              one row: how far back the first search looks, where the last search stopped, and the
--                             health of the login (so the doctor and the dashboard can say "the Gmail login expired").
--  * gmail_seen               every message that was looked at, by Gmail's message id, so one email is never handled
--                             twice even when two searches overlap.
--  * email_suggestions        what the reader proposes: "this email looks like an interview invitation for job 41:
--                             applied -> interview". A suggestion changes nothing by itself; accepting it (a later
--                             step) goes through the same status function as a click on the board, so it is recorded in
--                             the history and can be undone.
--
-- What is stored about an email is deliberately small: who it came from (the domain), the subject, the date and a short
-- excerpt. Never the whole message. The tables are not readable by the dashboard role (it has no table privileges); the
-- dashboard gets SECURITY DEFINER functions in a later step, like every other panel.

-- ---------------------------------------------------------------- the switch (off by default)
ALTER TABLE feature_settings ADD COLUMN gmail boolean NOT NULL DEFAULT false;

-- ---------------------------------------------------------------- the `mail` stage
DO $$
DECLARE c record;
BEGIN
  FOR c IN SELECT conname FROM pg_constraint
            WHERE conrelid = 'schedule_settings'::regclass AND contype = 'c'
              AND pg_get_constraintdef(oid) ~ 'collect.*score.*tailor.*digest'
  LOOP
    EXECUTE format('ALTER TABLE schedule_settings DROP CONSTRAINT %I', c.conname);
  END LOOP;
END $$;

ALTER TABLE schedule_settings
  ADD CONSTRAINT schedule_settings_name_check CHECK (name IN ('collect', 'score', 'tailor', 'digest', 'mail')),
  ADD CONSTRAINT schedule_mail_min_interval   CHECK (name <> 'mail' OR every_minutes >= 15);   -- be gentle with Gmail

-- Every 30 minutes, at most 20 messages opened per run. `enabled` is true: the switch above is what holds it back.
INSERT INTO schedule_settings (name, every_minutes, batch) VALUES ('mail', 30, 20) ON CONFLICT (name) DO NOTHING;

-- Same function as in 010, with `mail` after `digest` in the listing.
CREATE OR REPLACE FUNCTION schedule_status() RETURNS TABLE (
  name text, enabled boolean, every_minutes int, at_time time, batch int, model text, run_now boolean,
  last_run_at timestamptz, next_run_at timestamptz, timezone text)
LANGUAGE sql STABLE AS $$
  WITH tz AS (SELECT b.timezone AS z FROM budget_settings b WHERE b.id = 1)
  SELECT s.name, s.enabled, s.every_minutes, s.at_time, s.batch, s.model, s.run_now, s.last_run_at,
         CASE
           WHEN NOT s.enabled THEN NULL
           WHEN s.every_minutes IS NOT NULL AND s.last_run_at IS NULL THEN now()    -- never run: due at the next tick
           WHEN s.every_minutes IS NOT NULL THEN s.last_run_at + s.every_minutes * interval '1 minute'
           ELSE (  -- the next occurrence of at_time that has not run yet, in the configured timezone
             (CASE WHEN (now() AT TIME ZONE tz.z)::time < s.at_time
                        AND (s.last_run_at IS NULL OR (s.last_run_at AT TIME ZONE tz.z)::date < (now() AT TIME ZONE tz.z)::date)
                   THEN (now() AT TIME ZONE tz.z)::date
                   WHEN s.last_run_at IS NULL THEN (now() AT TIME ZONE tz.z)::date
                   WHEN (s.last_run_at AT TIME ZONE tz.z)::date < (now() AT TIME ZONE tz.z)::date THEN (now() AT TIME ZONE tz.z)::date
                   ELSE (now() AT TIME ZONE tz.z)::date + 1 END + s.at_time) AT TIME ZONE tz.z)
         END,
         tz.z
    FROM schedule_settings s, tz
   ORDER BY CASE s.name WHEN 'collect' THEN 1 WHEN 'score' THEN 2 WHEN 'tailor' THEN 3 WHEN 'digest' THEN 4 ELSE 5 END;
$$;

-- ---------------------------------------------------------------- state of the reader (one row)
CREATE TABLE gmail_state (
  id               int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  -- How far back the very first search looks, in days. The default reaches the replies to applications you made before
  -- the reader was switched on. from_now = true means "only mail that arrives after the first search": the first
  -- search then fetches nothing and just starts the clock.
  lookback_days    int     NOT NULL DEFAULT 60 CHECK (lookback_days BETWEEN 1 AND 365),
  from_now         boolean NOT NULL DEFAULT false,
  -- When the last successful search began; the next one looks from just before it (the overlap is harmless: gmail_seen
  -- drops what was handled). NULL = never searched yet.
  search_cursor    timestamptz,
  token_status     text    NOT NULL DEFAULT 'none' CHECK (token_status IN ('none', 'ok', 'expired', 'error')),
  token_checked_at timestamptz,
  last_run_at      timestamptz,
  last_error       text    CHECK (last_error IS NULL OR length(last_error) <= 300),
  updated_at       timestamptz NOT NULL DEFAULT now()
);
INSERT INTO gmail_state DEFAULT VALUES;

-- ---------------------------------------------------------------- every message that was looked at
CREATE TABLE gmail_seen (
  message_id text PRIMARY KEY CHECK (length(message_id) BETWEEN 1 AND 64),
  thread_id  text CHECK (thread_id IS NULL OR length(thread_id) <= 64),
  seen_at    timestamptz NOT NULL DEFAULT now(),
  -- suggested: a suggestion was written. noise: looked at, not about an application. unmatched: looked job-related but
  -- no application could be named with confidence. failed: could not be read or classified (counted, then parked).
  outcome    text NOT NULL CHECK (outcome IN ('suggested', 'noise', 'unmatched', 'failed')),
  job_id     bigint REFERENCES jobs (id) ON DELETE SET NULL
);
CREATE INDEX gmail_seen_seen_at_idx ON gmail_seen (seen_at);

-- ---------------------------------------------------------------- what the reader proposes
CREATE TABLE email_suggestions (
  id                 bigserial PRIMARY KEY,
  job_id             bigint NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
  message_id         text   NOT NULL UNIQUE CHECK (length(message_id) BETWEEN 1 AND 64),
  thread_id          text   CHECK (thread_id IS NULL OR length(thread_id) <= 64),
  received_at        timestamptz NOT NULL,
  from_domain        text   NOT NULL CHECK (length(from_domain) BETWEEN 1 AND 253),
  subject            text   NOT NULL CHECK (length(subject) <= 300),
  excerpt            text   NOT NULL CHECK (length(excerpt) <= 600),
  signal             text   NOT NULL CHECK (signal IN ('acknowledgement', 'assessment', 'interview', 'offer', 'rejection')),
  -- Only these four, and never a status after "offer": whether you accept or decline an offer is your decision, not
  -- something an email can prove. (They are statuses set_application_status() accepts.)
  proposed_status    text   NOT NULL CHECK (proposed_status IN ('applied', 'interview', 'offer', 'rejected')),
  status_at_proposal text   NOT NULL,      -- the application's status when this was written ('none' when it had no application)
  confidence         text   NOT NULL CHECK (confidence IN ('high', 'medium', 'low')),
  source             text   NOT NULL CHECK (source IN ('rules', 'model')),
  flags              text[] NOT NULL DEFAULT '{}' CHECK (cardinality(flags) <= 10),
  state              text   NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'accepted', 'dismissed', 'superseded')),
  decided_at         timestamptz,
  event_id           bigint REFERENCES application_events (id) ON DELETE SET NULL,   -- the history entry an accept created (so it can be undone)
  created_at         timestamptz NOT NULL DEFAULT now(),
  CHECK ((state = 'pending') = (decided_at IS NULL)),
  -- The status follows from the signal and from nothing else: code decides it, the classifier only names the signal.
  CHECK ((signal, proposed_status) IN (('acknowledgement', 'applied'), ('assessment', 'interview'), ('interview', 'interview'),
                                       ('offer', 'offer'), ('rejection', 'rejected')))
);
CREATE INDEX email_suggestions_state_idx ON email_suggestions (state, created_at DESC);
CREATE INDEX email_suggestions_job_idx   ON email_suggestions (job_id);

-- ---------------------------------------------------------------- one report for the CLI, the doctor and the dashboard
-- Counts and state only: no email text. (The scraper adds what it knows about .env, which SQL cannot see.)
CREATE FUNCTION gmail_overview() RETURNS jsonb
LANGUAGE sql STABLE AS $$
  SELECT jsonb_build_object(
    'lookback_days',    g.lookback_days,
    'from_now',         g.from_now,
    'search_cursor',    g.search_cursor,
    'token_status',     g.token_status,
    'token_checked_at', g.token_checked_at,
    'last_run_at',      g.last_run_at,
    'last_error',       g.last_error,
    'messages_seen',    (SELECT count(*) FROM gmail_seen),
    'pending',          (SELECT count(*) FROM email_suggestions WHERE state = 'pending'),
    'accepted',         (SELECT count(*) FROM email_suggestions WHERE state = 'accepted'),
    'dismissed',        (SELECT count(*) FROM email_suggestions WHERE state = 'dismissed'))
    FROM gmail_state g WHERE g.id = 1
$$;

REVOKE ALL ON FUNCTION gmail_overview() FROM PUBLIC;

-- ---------------------------------------------------------------- the dashboard's health panel knows the new stage
-- The same function as in 016, with two changes: the `mail` schedule is "feature off" while the Gmail switch is off (the
-- scheduler leaves it alone then, so its last run going stale is on purpose), and it is listed after the digest.
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
