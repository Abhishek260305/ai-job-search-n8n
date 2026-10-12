-- 020: undo a status change made on the dashboard (docs/dashboard.md, version 3a, "Undo").
--
-- A mistaken drop on the Board used to stay in the history for good: the job could be moved back, but the stats still
-- counted what it had briefly been. Now the last move can be taken back, as if it had never happened:
--
--  1. application_events remembers what each change started from (the job's status, the applied date, the review time
--     and note before it). Events made before this migration have none of that, so they cannot be undone.
--  2. dashboard_undo_status(job, expected, event): takes back the NEWEST history entry of a job, and only if it was made
--     on the dashboard, nothing has happened to the job since, and the job is still in the status that entry set. It
--     puts the application and the job back exactly as they were, and deletes the entry (the undo itself is not
--     recorded), so the stats never see the mistake. The entry before it becomes the newest, and may be undone in turn.
--  3. dashboard_job_events() now says which entry can be undone (`undoable`: only ever the newest).
--
-- An undo is refused if the application itself was changed since the entry (for example tailored again: new files), even
-- when no newer entry exists: the entry remembers a fingerprint of the application as the change left it.
-- dashboard_move_status() is the dashboard's status change, which also says which history entry it wrote (none when
-- nothing changed), so the "Undo" offered after a move is tied to that very entry.
--
-- SQLSTATEs: JD404 no such job, JD403 the job is scam-blocked, JD413 the history changed since the page last read it
-- (or there is nothing to undo), JD422 the entry cannot be undone (not made on the dashboard, too old, or it created an
-- application that has files).
-- Same as before: SECURITY DEFINER, granted to jobsearch_dashboard only, no table privileges.

ALTER TABLE application_events
  ADD COLUMN prev_job_status  text,
  ADD COLUMN prev_applied_at  timestamptz,
  ADD COLUMN prev_reviewed_at timestamptz,
  ADD COLUMN prev_review_note text,
  ADD COLUMN prev_updated_at  timestamptz,
  ADD COLUMN after_fingerprint text;

-- What an undo must not silently overwrite: the files and the variant of an application.
CREATE FUNCTION application_fingerprint(p_job_id bigint) RETURNS text LANGUAGE sql STABLE AS $$
  SELECT md5(concat_ws('|', variant, files_dir, resume_path, cover_letter_path, tailor_patch::text))
    FROM applications WHERE job_id = p_job_id
$$;

-- The trigger records the starting point too, and stays quiet while an undo restores it.
CREATE OR REPLACE FUNCTION record_application_event() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE v_source text := nullif(current_setting('jobsearch.source', true), '');
        v_job text;
BEGIN
  -- the restore of an undo is not a change to remember. The marker is this transaction's id, so nobody can switch the
  -- history off by setting a plain value.
  IF current_setting('jobsearch.undo', true) = txid_current()::text THEN
    RETURN NULL;
  END IF;
  -- the job's own status is moved by set_application_status() after the application row, so it is still the old one here
  SELECT status INTO v_job FROM jobs WHERE id = NEW.job_id;
  IF TG_OP = 'INSERT' THEN
    INSERT INTO application_events (job_id, from_status, to_status, note, source, prev_job_status, after_fingerprint)
    VALUES (NEW.job_id, NULL, NEW.status, NEW.review_note, CASE WHEN v_source = 'dashboard' THEN v_source END, v_job,
            md5(concat_ws('|', NEW.variant, NEW.files_dir, NEW.resume_path, NEW.cover_letter_path, NEW.tailor_patch::text)));
  ELSE
    -- a note recorded without a status change is an event too (from and to are the same)
    INSERT INTO application_events (job_id, from_status, to_status, note, source,
                                    prev_job_status, prev_applied_at, prev_reviewed_at, prev_review_note, prev_updated_at,
                                    after_fingerprint)
    VALUES (NEW.job_id, OLD.status, NEW.status,
            CASE WHEN NEW.review_note IS DISTINCT FROM OLD.review_note THEN NEW.review_note END,
            CASE WHEN v_source = 'dashboard' THEN v_source END,
            v_job, OLD.applied_at, OLD.reviewed_at, OLD.review_note, OLD.updated_at,
            md5(concat_ws('|', NEW.variant, NEW.files_dir, NEW.resume_path, NEW.cover_letter_path, NEW.tailor_patch::text)));
  END IF;
  RETURN NULL;
END;
$$;

-- Whether the newest entry of a job can be taken back right now (the same checks as the undo itself, without raising).
CREATE FUNCTION entry_is_undoable(p_event_id bigint) RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT coalesce((
    SELECT e.id = (SELECT max(n.id) FROM application_events n WHERE n.job_id = e.job_id)
           AND e.source = 'dashboard'
           AND e.prev_job_status IS NOT NULL
           AND a.status = e.to_status
           AND e.after_fingerprint IS NOT DISTINCT FROM application_fingerprint(e.job_id)
           AND j.status <> 'scam_blocked'
           AND (e.from_status IS NOT NULL OR (a.variant = 'base' AND a.resume_path IS NULL AND a.cover_letter_path IS NULL))
      FROM application_events e
      JOIN jobs j ON j.id = e.job_id
      JOIN applications a ON a.job_id = e.job_id
     WHERE e.id = p_event_id), false)
$$;

CREATE OR REPLACE FUNCTION dashboard_job_events(p_job_id bigint) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM jobs WHERE id = p_job_id) THEN
    RAISE EXCEPTION 'no job %', p_job_id USING ERRCODE = 'JD404';
  END IF;
  RETURN coalesce((SELECT jsonb_agg(jsonb_build_object(
            'id', e.id, 'from_status', e.from_status, 'to_status', e.to_status, 'note', e.note,
            'source', e.source, 'at', e.at,
            -- only the newest entry can ever be offered: the others are not even looked at
            'undoable', CASE WHEN e.id = (SELECT max(n.id) FROM application_events n WHERE n.job_id = p_job_id)
                             THEN entry_is_undoable(e.id) ELSE false END) ORDER BY e.at, e.id)
          FROM (SELECT * FROM application_events WHERE job_id = p_job_id ORDER BY at DESC, id DESC LIMIT 200) e), '[]');
END;
$$;

CREATE FUNCTION dashboard_undo_status(p_job_id bigint, p_expected text, p_event_id bigint DEFAULT NULL) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v_job jobs%ROWTYPE; v_app applications%ROWTYPE; v_ev application_events%ROWTYPE;
BEGIN
  SELECT * INTO v_job FROM jobs WHERE id = p_job_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'no job %', p_job_id USING ERRCODE = 'JD404'; END IF;
  IF v_job.status = 'scam_blocked' THEN RAISE EXCEPTION 'job % is scam-blocked', p_job_id USING ERRCODE = 'JD403'; END IF;
  SELECT * INTO v_app FROM applications WHERE job_id = p_job_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'there is nothing to undo' USING ERRCODE = 'JD413'; END IF;
  SELECT * INTO v_ev FROM application_events WHERE job_id = p_job_id ORDER BY id DESC LIMIT 1;
  IF NOT FOUND THEN RAISE EXCEPTION 'there is nothing to undo' USING ERRCODE = 'JD413'; END IF;

  IF (p_event_id IS NOT NULL AND v_ev.id <> p_event_id) OR p_expected IS NULL
     OR v_ev.to_status <> p_expected OR v_app.status <> v_ev.to_status
     OR v_ev.after_fingerprint IS DISTINCT FROM application_fingerprint(p_job_id) THEN
    RAISE EXCEPTION 'the history of job % changed', p_job_id USING ERRCODE = 'JD413';
  END IF;
  IF v_ev.source IS DISTINCT FROM 'dashboard' THEN
    RAISE EXCEPTION 'only a change made on the dashboard can be undone here' USING ERRCODE = 'JD422';
  END IF;
  IF v_ev.prev_job_status IS NULL THEN
    RAISE EXCEPTION 'this entry is from before undo existed and cannot be undone' USING ERRCODE = 'JD422';
  END IF;

  PERFORM set_config('jobsearch.undo', txid_current()::text, true);
  IF v_ev.from_status IS NULL THEN
    -- the move created the application: take it away again (never one that has files)
    IF v_app.variant IS DISTINCT FROM 'base' OR v_app.resume_path IS NOT NULL OR v_app.cover_letter_path IS NOT NULL THEN
      RAISE EXCEPTION 'this application has files and cannot be removed' USING ERRCODE = 'JD422';
    END IF;
    DELETE FROM applications WHERE job_id = p_job_id;
  ELSE
    UPDATE applications
       SET status = v_ev.from_status, applied_at = v_ev.prev_applied_at, reviewed_at = v_ev.prev_reviewed_at,
           review_note = v_ev.prev_review_note, updated_at = coalesce(v_ev.prev_updated_at, now())
     WHERE job_id = p_job_id;
  END IF;
  UPDATE jobs SET status = v_ev.prev_job_status WHERE id = p_job_id;
  DELETE FROM application_events WHERE id = v_ev.id;
  PERFORM set_config('jobsearch.undo', '', true);

  RETURN jsonb_build_object('id', p_job_id,
                            'status', coalesce((SELECT status FROM applications WHERE job_id = p_job_id), v_ev.prev_job_status));
END;
$$;

-- The dashboard's status change, as dashboard_set_status() (same arguments, same checks), that also names the history entry
-- it wrote: {status, event_id}, with event_id null when nothing was recorded (the same status again, with no new note).
CREATE FUNCTION dashboard_move_status(p_job_id bigint, p_status text, p_expected text, p_note text DEFAULT NULL)
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v_before bigint; v_after bigint; v_status text;
BEGIN
  PERFORM 1 FROM jobs WHERE id = p_job_id FOR UPDATE;     -- nobody else writes this job's history while this runs
  SELECT max(id) INTO v_before FROM application_events WHERE job_id = p_job_id;
  v_status := dashboard_set_status(p_job_id, p_status, p_expected, p_note);
  SELECT max(id) INTO v_after FROM application_events WHERE job_id = p_job_id;
  RETURN jsonb_build_object('status', v_status,
                            'event_id', CASE WHEN v_after IS DISTINCT FROM v_before THEN v_after END);
END;
$$;

REVOKE ALL ON FUNCTION entry_is_undoable(bigint), application_fingerprint(bigint), dashboard_undo_status(bigint, text, bigint),
                       dashboard_move_status(bigint, text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION dashboard_undo_status(bigint, text, bigint), dashboard_move_status(bigint, text, text, text) TO jobsearch_dashboard;
