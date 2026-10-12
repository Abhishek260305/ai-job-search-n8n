-- 008: optional parts. Nothing beyond collecting and scoring is forced on the user.
--
--  * feature_settings: switches for the optional stages (tailoring, Telegram, the daily digest).
--  * tailor_settings.approval gains 'cli': no push message, review at the command line.
--  * set_application_status(): edit an application's status by hand, with or without Gmail tracking
--    and with or without a tailored application (a "base" row is created when there is none).
--  * review_application() now works for a job that was never tailored (it records the decision on
--    a "base" row), so approve / skip / applied are usable on the stop-at-scoring path.

-- ---------------------------------------------------------------- feature switches
CREATE TABLE feature_settings (
  id         int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  tailoring  boolean NOT NULL DEFAULT true,   -- master switch for resume + cover-letter generation
  telegram   boolean NOT NULL DEFAULT true,   -- allow Telegram (it also needs a bot token and chat id in .env)
  digest     boolean NOT NULL DEFAULT true,   -- the daily digest message (it needs Telegram)
  updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO feature_settings DEFAULT VALUES;

-- ---------------------------------------------------------------- approval: review | cli | auto
ALTER TABLE tailor_settings DROP CONSTRAINT IF EXISTS tailor_settings_approval_check;
ALTER TABLE tailor_settings ADD CONSTRAINT tailor_settings_approval_check CHECK (approval IN ('review', 'cli', 'auto'));

-- ---------------------------------------------------------------- manual status editing
-- The user is the source of truth here, so any status may be set from any other. Creates a "base"
-- application (no files) when the job has none. applied_at is set the first time the application
-- reaches a post-submission status. Jobs that are scam-blocked keep their job status.
CREATE OR REPLACE FUNCTION set_application_status(p_job_id bigint, p_status text, p_note text DEFAULT NULL) RETURNS text
LANGUAGE plpgsql AS $$
DECLARE v_submitted boolean;
BEGIN
  IF p_status NOT IN ('approved', 'skipped', 'applied', 'in_progress', 'interview', 'offer', 'rejected', 'withdrawn') THEN
    RAISE EXCEPTION 'unknown status %', p_status;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM jobs WHERE id = p_job_id) THEN RAISE EXCEPTION 'no job %', p_job_id; END IF;
  v_submitted := p_status IN ('applied', 'in_progress', 'interview', 'offer', 'rejected', 'withdrawn');

  INSERT INTO applications (job_id, status, variant, reviewed_at, review_note, applied_at)
  VALUES (p_job_id, p_status, 'base', now(), p_note, CASE WHEN v_submitted THEN now() END)
  ON CONFLICT (job_id) DO UPDATE SET
    status      = EXCLUDED.status,
    reviewed_at = now(),
    review_note = coalesce(EXCLUDED.review_note, applications.review_note),
    applied_at  = CASE WHEN v_submitted THEN coalesce(applications.applied_at, now()) ELSE applications.applied_at END,
    updated_at  = now();

  UPDATE jobs SET status = CASE p_status WHEN 'approved' THEN 'approved' WHEN 'skipped' THEN 'skipped' ELSE 'applied' END
   WHERE id = p_job_id AND status <> 'scam_blocked';
  RETURN p_status;
END;
$$;

-- Approve / skip / mark applied. Each only moves forward from a state where it makes sense. A job with
-- no application yet (never tailored, or tailoring is off) gets one recorded by hand.
CREATE OR REPLACE FUNCTION review_application(p_job_id bigint, p_action text, p_note text DEFAULT NULL) RETURNS text
LANGUAGE plpgsql AS $$
DECLARE v_new text; v_cur text;
BEGIN
  IF p_action NOT IN ('approve', 'skip', 'applied') THEN RAISE EXCEPTION 'unknown action %', p_action; END IF;
  SELECT status INTO v_cur FROM applications WHERE job_id = p_job_id;
  IF v_cur IS NULL THEN
    RETURN set_application_status(p_job_id, CASE p_action WHEN 'approve' THEN 'approved' WHEN 'skip' THEN 'skipped' ELSE 'applied' END, p_note);
  END IF;
  v_new := CASE p_action
             WHEN 'approve' THEN CASE WHEN v_cur IN ('drafted', 'skipped') THEN 'approved' END
             WHEN 'skip'    THEN CASE WHEN v_cur IN ('drafted', 'approved') THEN 'skipped' END
             WHEN 'applied' THEN CASE WHEN v_cur IN ('drafted', 'approved') THEN 'applied' END
             ELSE NULL END;
  IF v_new IS NULL THEN RAISE EXCEPTION 'cannot % an application that is %', p_action, v_cur; END IF;
  UPDATE applications SET status = v_new, reviewed_at = now(), review_note = coalesce(p_note, review_note),
         applied_at = CASE WHEN v_new = 'applied' THEN now() ELSE applied_at END, updated_at = now()
   WHERE job_id = p_job_id;
  UPDATE jobs SET status = v_new WHERE id = p_job_id AND status IN ('shortlisted', 'tailored', 'approved', 'skipped', 'applied');
  RETURN v_new;
END;
$$;
