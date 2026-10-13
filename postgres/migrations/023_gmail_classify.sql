-- 023: Gmail status tracking, step 6d (docs/gmail.md): what the model step needs from the database.
--
-- The rules of step 6c leave some matched emails "unclear" (gmail_seen.outcome = 'unclear'). Step 6d asks Claude (Haiku) about
-- those, under the same guards as scoring and tailoring (docs/runaway-protection.md):
--
--  * every attempt is COUNTED when it is claimed (classify_claims), not only when it ends in a bad answer, so an email whose
--    answer is never recorded (an outage after the reply, a crash) is asked about a bounded number of times and then PARKED:
--    it stays 'unclear', is never claimed again, and is counted as parked in the overview until `gmail rescan` clears it;
--  * an attempt that provably cost nothing (the email could not be fetched, the API was down, the budget was paused) is
--    refunded, so an outage never parks a healthy email;
--  * a lease (classify_claimed_at) keeps two runs from asking about the same email at once;
--  * the settled answer goes through gmail_record_result() (022), so the same "one pending suggestion per job and status, a
--    more trustworthy email replaces a worse one" rules apply to a suggestion the model made.
--  * gmail_classified_today(): how many model calls were logged today (in the budget timezone), for a per-day limit that is
--    independent of the money caps.
--  * gmail_can_get_reply(): the rule of gmail_candidates() (022) as a predicate on a job's and an application's status. An
--    unclear email about a job that is scam-blocked, skipped or not shortlisted is not claimed and costs nothing; it stays as it
--    is (and returns to the queue if the job becomes a candidate again).
--  * gmail_rescan() (replaced here) keeps what the model already settled as noise: a rescan re-opens what the RULES left aside,
--    not the emails the model was already paid to look at. Parked emails (unclear, claims used up) are re-opened, as documented.
-- The functions are for the scraper's own role; the dashboard role cannot run them (like the other Gmail functions).

ALTER TABLE gmail_seen
  ADD COLUMN classify_claims     int NOT NULL DEFAULT 0 CHECK (classify_claims BETWEEN 0 AND 20),
  ADD COLUMN classify_claimed_at timestamptz;

CREATE FUNCTION gmail_can_get_reply(p_job_status text, p_app_status text) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
  SELECT coalesce(p_job_status IS DISTINCT FROM 'scam_blocked'
     AND ( p_app_status IN ('applied', 'in_progress', 'interview', 'offer', 'rejected', 'withdrawn')
        OR (p_job_status IN ('shortlisted', 'tailored', 'approved') AND coalesce(p_app_status, 'none') IN ('none', 'drafted', 'approved')) ), false)
$$;

CREATE FUNCTION gmail_claim_unclear(p_limit int, p_max_claims int DEFAULT 3, p_lease interval DEFAULT interval '20 minutes')
RETURNS TABLE (message_id text, thread_id text, job_id bigint, company text, title text, job_status text, app_status text)
LANGUAGE sql AS $$
  WITH c AS (
    UPDATE gmail_seen s SET classify_claims = s.classify_claims + 1, classify_claimed_at = now()
     WHERE s.message_id IN (
       SELECT u.message_id FROM gmail_seen u
        WHERE u.outcome = 'unclear' AND u.job_id IS NOT NULL
          AND u.classify_claims < p_max_claims
          AND (u.classify_claimed_at IS NULL OR u.classify_claimed_at < now() - p_lease)
          AND EXISTS (SELECT 1 FROM jobs j LEFT JOIN applications a ON a.job_id = j.id
                       WHERE j.id = u.job_id AND gmail_can_get_reply(j.status, a.status))
        ORDER BY u.seen_at DESC, u.message_id
        LIMIT greatest(0, least(p_limit, 50))
        FOR UPDATE SKIP LOCKED)
    RETURNING s.message_id, s.thread_id, s.job_id)
  SELECT c.message_id, c.thread_id, c.job_id, j.company, j.title, j.status, a.status
    FROM c JOIN jobs j ON j.id = c.job_id LEFT JOIN applications a ON a.job_id = c.job_id
   ORDER BY c.message_id
$$;

-- Give back a claim that cost nothing: the email is not penalised.
CREATE FUNCTION gmail_release_classify_claim(p_message_id text) RETURNS int
LANGUAGE plpgsql AS $$
DECLARE n int;
BEGIN
  UPDATE gmail_seen SET classify_claims = greatest(classify_claims - 1, 0), classify_claimed_at = NULL
   WHERE message_id = p_message_id AND outcome = 'unclear';
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END;
$$;

-- Settle one 'unclear' email: noise (the model found no signal), suggested (with a suggestion) or failed. Nothing happens, and
-- 'gone' is returned, when the email is no longer 'unclear' (a rescan cleared it, or another run settled it first) or now belongs to another job.
CREATE FUNCTION gmail_settle_unclear(p_message_id text, p_thread_id text, p_outcome text, p_job_id bigint, p_suggestion jsonb DEFAULT NULL)
RETURNS text
LANGUAGE plpgsql AS $$
DECLARE
  v_job     bigint;
  v_outcome text;
BEGIN
  IF p_outcome NOT IN ('suggested', 'noise', 'failed') THEN
    RAISE EXCEPTION 'an unclear email settles as suggested, noise or failed';
  END IF;
  IF (p_outcome = 'suggested') <> (p_suggestion IS NOT NULL) THEN
    RAISE EXCEPTION 'the outcome "suggested" goes with a suggestion, and only with one';
  END IF;
  SELECT job_id INTO v_job FROM gmail_seen WHERE message_id = p_message_id AND outcome = 'unclear' FOR UPDATE;
  IF NOT FOUND THEN
    RETURN 'gone';
  END IF;
  IF p_job_id IS DISTINCT FROM v_job THEN
    RETURN 'gone';               -- a rescan and a new search gave the email another job while it was being classified: not this answer's email
  END IF;
  v_outcome := gmail_record_result(p_message_id, p_thread_id, p_outcome, p_job_id, p_suggestion);
  UPDATE gmail_seen SET outcome = v_outcome, classify_claimed_at = NULL WHERE message_id = p_message_id;
  RETURN v_outcome;
END;
$$;

-- Waiting = the next run may ask about it; parked = it used all its claims and waits for a rescan.
CREATE FUNCTION gmail_unclear_counts(p_max_claims int DEFAULT 3) RETURNS jsonb
LANGUAGE sql STABLE AS $$
  SELECT jsonb_build_object(
    'waiting', count(*) FILTER (WHERE s.classify_claims < p_max_claims),
    'parked',  count(*) FILTER (WHERE s.classify_claims >= p_max_claims))
    FROM gmail_seen s JOIN jobs j ON j.id = s.job_id LEFT JOIN applications a ON a.job_id = j.id
   WHERE s.outcome = 'unclear' AND gmail_can_get_reply(j.status, a.status)
$$;

CREATE FUNCTION gmail_classified_today() RETURNS int
LANGUAGE sql STABLE AS $$
  SELECT count(*)::int FROM llm_usage u, budget_settings b
   WHERE b.id = 1 AND u.provider = 'anthropic' AND u.purpose = 'mail_classify'
     AND u.at >= (date_trunc('day', now() AT TIME ZONE b.timezone)) AT TIME ZONE b.timezone
$$;

CREATE OR REPLACE FUNCTION gmail_rescan() RETURNS int
LANGUAGE plpgsql AS $$
DECLARE n int;
BEGIN
  UPDATE gmail_state SET search_cursor = NULL, updated_at = now() WHERE id = 1;
  -- noise the model settled (it was claimed at least once) stays: asking again would pay again for the same answer
  DELETE FROM gmail_seen WHERE outcome IN ('unmatched', 'unclear') OR (outcome = 'noise' AND classify_claims = 0);
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END;
$$;

REVOKE ALL ON FUNCTION gmail_can_get_reply(text, text), gmail_claim_unclear(int, int, interval), gmail_release_classify_claim(text),
  gmail_settle_unclear(text, text, text, bigint, jsonb), gmail_unclear_counts(int), gmail_classified_today() FROM PUBLIC;
