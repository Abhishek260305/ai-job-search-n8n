-- 024: Gmail status tracking, step 6e (docs/gmail.md): the scheduled stage reads mail, and tells you about it.
--
--  * gmail_state.last_summary   what the last run did, in counts only (never an email's text): opened, suggested, ...
--  * gmail_state.early_stops    how many runs in a row ended early (Gmail limiting requests, Claude not answering, a deadline):
--                               the doctor says so when a run keeps failing; a clean run resets it
--  * gmail_state.login_alerted_at  when the digest told you the Google login EXPIRED (so it is said once, not every hour; a working
--                               login clears it). A rate limit or an outage is not an expired login and never raises it.
--  * email_suggestions.notified_at  when the digest told you about a suggestion (so each is announced once)
--  * gmail_record_run()         stores the summary and the early-stop count
--  * gmail_digest()             what the Telegram digest says about mail: the new suggestions (company, job, what the reply looks
--                               like, what it would change, how sure; never the subject or the text) and whether the login alert is
--                               due. Empty while the Gmail part is off.
--  * gmail_digest_sent()        marks what the digest told you
--  * gmail_overview()           (replaced) also reports the last summary and the early-stop count
-- The functions are for the scraper's own role; the dashboard role cannot run them.

ALTER TABLE gmail_state
  ADD COLUMN last_summary     jsonb CHECK (last_summary IS NULL OR octet_length(last_summary::text) <= 2000),
  ADD COLUMN early_stops      int NOT NULL DEFAULT 0 CHECK (early_stops >= 0),
  ADD COLUMN login_alerted_at timestamptz;
ALTER TABLE email_suggestions ADD COLUMN notified_at timestamptz;
CREATE INDEX email_suggestions_unnotified_idx ON email_suggestions (created_at) WHERE state = 'pending' AND notified_at IS NULL;

CREATE FUNCTION gmail_record_run(p_summary jsonb, p_early boolean) RETURNS void
LANGUAGE sql AS $$
  UPDATE gmail_state SET last_summary = p_summary, last_run_at = now(), updated_at = now(),
         early_stops = CASE WHEN p_early THEN least(early_stops + 1, 1000) ELSE 0 END
   WHERE id = 1
$$;

CREATE OR REPLACE FUNCTION gmail_overview() RETURNS jsonb
LANGUAGE sql STABLE AS $$
  SELECT jsonb_build_object(
    'lookback_days',    g.lookback_days,
    'from_now',         g.from_now,
    'search_cursor',    g.search_cursor,
    'token_status',     g.token_status,
    'token_checked_at', g.token_checked_at,
    'last_run_at',      g.last_run_at,
    'last_error',       g.last_error,
    'last_summary',     g.last_summary,
    'early_stops',      g.early_stops,
    'messages_seen',    (SELECT count(*) FROM gmail_seen),
    'pending',          (SELECT count(*) FROM email_suggestions WHERE state = 'pending'),
    'accepted',         (SELECT count(*) FROM email_suggestions WHERE state = 'accepted'),
    'dismissed',        (SELECT count(*) FROM email_suggestions WHERE state = 'dismissed'))
    FROM gmail_state g WHERE g.id = 1
$$;

CREATE FUNCTION gmail_digest(p_limit int DEFAULT 5) RETURNS jsonb
LANGUAGE sql STABLE AS $$
  WITH f AS (SELECT gmail FROM feature_settings WHERE id = 1),
  n AS (
    SELECT s.id, s.signal, s.proposed_status, s.status_at_proposal, s.confidence, s.source, s.flags, j.company, j.title
      FROM email_suggestions s JOIN jobs j ON j.id = s.job_id
     WHERE s.state = 'pending' AND s.notified_at IS NULL
  )
  SELECT CASE WHEN NOT (SELECT gmail FROM f) THEN '{"suggestions": [], "total_new": 0, "login_alert": false}'::jsonb
         ELSE jsonb_build_object(
           'suggestions', coalesce((SELECT jsonb_agg(to_jsonb(x)) FROM (SELECT * FROM n ORDER BY id DESC LIMIT greatest(1, least(p_limit, 20))) x), '[]'::jsonb),
           'total_new',   (SELECT count(*) FROM n),
           'login_alert', (SELECT g.token_status = 'expired' AND g.login_alerted_at IS NULL FROM gmail_state g WHERE g.id = 1))
         END
$$;

CREATE FUNCTION gmail_digest_sent(p_ids bigint[], p_login boolean DEFAULT false) RETURNS int
LANGUAGE plpgsql AS $$
DECLARE n int;
BEGIN
  UPDATE email_suggestions SET notified_at = now() WHERE id = ANY(coalesce(p_ids, '{}')) AND notified_at IS NULL;
  GET DIAGNOSTICS n = ROW_COUNT;
  IF p_login THEN
    UPDATE gmail_state SET login_alerted_at = now() WHERE id = 1 AND token_status = 'expired' AND login_alerted_at IS NULL;
  END IF;
  RETURN n;
END;
$$;

REVOKE ALL ON FUNCTION gmail_record_run(jsonb, boolean), gmail_digest(int), gmail_digest_sent(bigint[], boolean) FROM PUBLIC;
