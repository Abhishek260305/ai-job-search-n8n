-- 022: Gmail status tracking, step 6c (docs/gmail.md): what the search needs from the database.
--
--  * gmail_seen.outcome gains 'unclear': an email the rules matched to an application but could not classify. It is recorded so
--    it is not opened again every run, and step 6d (Claude) picks those rows up.
--  * gmail_candidates(): the applications a reply could be about, newest activity first. Submitted applications (applied,
--    in progress, interview, offer, and the final rejected / withdrawn: a late email about one is a conflict worth showing)
--    and jobs that were shortlisted, tailored or approved but never marked applied (a person may have applied by hand and
--    never said so: an acknowledgement then proposes "applied"). Never a skipped job and never a scam-blocked one.
--  * gmail_record_result(): record one email's outcome and, when it has one, its suggestion, in one step. Re-running it for the
--    same message changes nothing. There is at most ONE pending suggestion per job and status (a unique index, so two runs at once
--    cannot make two): three "thanks for applying" emails make one. When a later email is MORE trustworthy than the pending one
--    (higher confidence), it replaces it, so a verified email is never lost behind an earlier spoof; otherwise it is recorded as
--    looked at and changes nothing.
--  * gmail_rescan(): forget where the search got to and re-open the emails that were looked at but not acted on (noise, unmatched,
--    unclear), so mail from before an application became a candidate is read again. Suggestions already made are untouched.
--  Both functions are for the scraper's own role; the dashboard role cannot run them, like gmail_overview().

ALTER TABLE gmail_seen DROP CONSTRAINT gmail_seen_outcome_check;
ALTER TABLE gmail_seen ADD CONSTRAINT gmail_seen_outcome_check
  CHECK (outcome IN ('suggested', 'noise', 'unmatched', 'unclear', 'failed'));
CREATE INDEX gmail_seen_unclear_idx ON gmail_seen (seen_at) WHERE outcome = 'unclear';
CREATE UNIQUE INDEX email_suggestions_one_pending ON email_suggestions (job_id, proposed_status) WHERE state = 'pending';

CREATE FUNCTION gmail_candidates(p_limit int DEFAULT 300) RETURNS TABLE (
  job_id bigint, company text, title text, job_status text, app_status text, last_activity timestamptz)
LANGUAGE sql STABLE AS $$
  SELECT j.id, j.company, j.title, j.status, a.status,
         greatest(j.first_seen, coalesce(a.updated_at, j.first_seen))
    FROM jobs j
    LEFT JOIN applications a ON a.job_id = j.id
   WHERE j.status <> 'scam_blocked'
     AND ( a.status IN ('applied', 'in_progress', 'interview', 'offer', 'rejected', 'withdrawn')
        OR (j.status IN ('shortlisted', 'tailored', 'approved') AND coalesce(a.status, 'none') IN ('none', 'drafted', 'approved')) )
   ORDER BY coalesce(a.status IN ('applied', 'in_progress', 'interview', 'offer'), false) DESC,
            greatest(j.first_seen, coalesce(a.updated_at, j.first_seen)) DESC, j.id DESC
   LIMIT greatest(1, least(p_limit, 1000))
$$;

CREATE FUNCTION gmail_record_result(p_message_id text, p_thread_id text, p_outcome text, p_job_id bigint, p_suggestion jsonb DEFAULT NULL)
RETURNS text
LANGUAGE plpgsql AS $$
DECLARE
  v_outcome  text := p_outcome;
  v_inserted bigint;
  v_pending  record;
  v_rank     int;
  v_old_rank int;
BEGIN
  IF p_suggestion IS NOT NULL THEN
    IF p_outcome <> 'suggested' OR p_job_id IS NULL THEN
      RAISE EXCEPTION 'a suggestion needs the outcome "suggested" and a job';
    END IF;
    v_rank := CASE p_suggestion->>'confidence' WHEN 'high' THEN 2 WHEN 'medium' THEN 1 ELSE 0 END;
    SELECT id, confidence INTO v_pending FROM email_suggestions
     WHERE job_id = p_job_id AND state = 'pending' AND proposed_status = p_suggestion->>'proposed_status'
     FOR UPDATE;
    IF FOUND THEN
      v_old_rank := CASE v_pending.confidence WHEN 'high' THEN 2 WHEN 'medium' THEN 1 ELSE 0 END;
    END IF;
    IF FOUND AND NOT EXISTS (SELECT 1 FROM email_suggestions WHERE message_id = p_message_id) THEN
      IF v_rank > v_old_rank THEN
        -- a more trustworthy email for the same thing: it takes the pending suggestion's place
        UPDATE email_suggestions SET
               message_id = p_message_id, thread_id = p_thread_id, received_at = (p_suggestion->>'received_at')::timestamptz,
               from_domain = p_suggestion->>'from_domain', subject = p_suggestion->>'subject', excerpt = p_suggestion->>'excerpt',
               signal = p_suggestion->>'signal', status_at_proposal = p_suggestion->>'status_at_proposal',
               confidence = p_suggestion->>'confidence', source = coalesce(p_suggestion->>'source', 'rules'),
               flags = ARRAY(SELECT jsonb_array_elements_text(coalesce(p_suggestion->'flags', '[]'::jsonb))), created_at = now()
         WHERE id = v_pending.id
        RETURNING id INTO v_inserted;
      END IF;
    ELSIF NOT FOUND THEN
      INSERT INTO email_suggestions (job_id, message_id, thread_id, received_at, from_domain, subject, excerpt, signal,
                                     proposed_status, status_at_proposal, confidence, source, flags)
      VALUES (p_job_id, p_message_id, p_thread_id, (p_suggestion->>'received_at')::timestamptz, p_suggestion->>'from_domain',
              p_suggestion->>'subject', p_suggestion->>'excerpt', p_suggestion->>'signal', p_suggestion->>'proposed_status',
              p_suggestion->>'status_at_proposal', p_suggestion->>'confidence', coalesce(p_suggestion->>'source', 'rules'),
              ARRAY(SELECT jsonb_array_elements_text(coalesce(p_suggestion->'flags', '[]'::jsonb))))
      ON CONFLICT DO NOTHING
      RETURNING id INTO v_inserted;
    END IF;
    IF v_inserted IS NULL THEN
      v_outcome := 'noise';        -- an equal or better suggestion is already waiting, or this email was already recorded
    END IF;
  END IF;
  INSERT INTO gmail_seen (message_id, thread_id, outcome, job_id)
  VALUES (p_message_id, p_thread_id, v_outcome, p_job_id)
  ON CONFLICT (message_id) DO NOTHING;
  RETURN v_outcome;
END;
$$;

CREATE FUNCTION gmail_rescan() RETURNS int
LANGUAGE plpgsql AS $$
DECLARE n int;
BEGIN
  UPDATE gmail_state SET search_cursor = NULL, updated_at = now() WHERE id = 1;
  DELETE FROM gmail_seen WHERE outcome IN ('noise', 'unmatched', 'unclear');
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END;
$$;

REVOKE ALL ON FUNCTION gmail_candidates(int), gmail_record_result(text, text, text, bigint, jsonb), gmail_rescan() FROM PUBLIC;
