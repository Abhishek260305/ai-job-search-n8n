-- 006: tailoring (resume patch + cover letter), review/approval, Telegram polling state.

-- ---------------------------------------------------------------- settings
-- One row. All knobs the user may want to change without touching code.
CREATE TABLE tailor_settings (
  id           int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  mode         text NOT NULL DEFAULT 'auto'   CHECK (mode IN ('auto', 'manual')),
  -- Score band: tailor jobs scoring at or above min_score (NULL = follow your search threshold).
  -- At or above max_score the base resume is already close, so it is used as-is (NULL = never).
  min_score    int CHECK (min_score BETWEEN 0 AND 100),
  max_score    int CHECK (max_score BETWEEN 0 AND 101),
  daily_limit  int CHECK (daily_limit >= 0),   -- NULL = no limit (the spending caps still apply)
  approval     text NOT NULL DEFAULT 'review' CHECK (approval IN ('review', 'auto')),
  model        text NOT NULL DEFAULT 'claude-sonnet-5-5',
  cover_letter boolean NOT NULL DEFAULT true,
  max_pages    int NOT NULL DEFAULT 1 CHECK (max_pages BETWEEN 1 AND 2),
  updated_at   timestamptz NOT NULL DEFAULT now(),
  CHECK (min_score IS NULL OR max_score IS NULL OR min_score < max_score)
);
INSERT INTO tailor_settings DEFAULT VALUES;

-- ---------------------------------------------------------------- applications
ALTER TABLE applications DROP CONSTRAINT IF EXISTS applications_status_check;
ALTER TABLE applications ADD CONSTRAINT applications_status_check CHECK (status IN (
  'drafted', 'approved', 'skipped', 'applied', 'in_progress', 'interview', 'offer', 'rejected', 'withdrawn'));

ALTER TABLE applications
  ADD COLUMN variant           text NOT NULL DEFAULT 'tailored' CHECK (variant IN ('tailored', 'base')),
  ADD COLUMN files_dir         text,
  ADD COLUMN model             text,
  ADD COLUMN cost_usd          numeric(12, 6) NOT NULL DEFAULT 0,
  ADD COLUMN requirements      jsonb NOT NULL DEFAULT '[]',   -- coverage: matched / bridged / gap
  ADD COLUMN audit             jsonb NOT NULL DEFAULT '{}',   -- grounding audit result
  ADD COLUMN needs_attention   boolean NOT NULL DEFAULT false,
  ADD COLUMN attention_reason  text,
  ADD COLUMN reviewed_at       timestamptz,
  ADD COLUMN review_note       text;

-- ---------------------------------------------------------------- tailoring claims (same lease idea as scoring)
ALTER TABLE jobs
  ADD COLUMN tailoring_claimed_at timestamptz,
  ADD COLUMN tailor_attempts      int NOT NULL DEFAULT 0;

-- Shortlisted jobs with no application yet, best first, not claimed by another run and not failed
-- too often. p_min_score is resolved by the caller (settings or the search threshold).
CREATE OR REPLACE FUNCTION claim_jobs_to_tailor(
  p_limit int, p_min_score int, p_max_attempts int DEFAULT 2, p_lease interval DEFAULT interval '20 minutes'
) RETURNS TABLE (id bigint, title text, company text, location text, description text, canonical_url text,
                 score int, verdict text, strengths jsonb, gaps jsonb, gates jsonb, posted_at timestamptz)
LANGUAGE sql AS $$
  UPDATE jobs j SET tailoring_claimed_at = now()
   WHERE j.id IN (
     SELECT c.id FROM jobs c
      WHERE c.status = 'shortlisted'
        AND coalesce(c.scam_risk, 'low') <> 'high'
        AND c.score >= p_min_score
        AND c.tailor_attempts < p_max_attempts
        AND (c.tailoring_claimed_at IS NULL OR c.tailoring_claimed_at < now() - p_lease)
        AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.job_id = c.id)
      ORDER BY c.score DESC, c.id
      LIMIT p_limit
      FOR UPDATE SKIP LOCKED)
  RETURNING j.id, j.title, j.company, j.location, j.description, j.canonical_url,
            j.score, j.verdict, j.strengths, j.gaps, j.gates, j.posted_at
$$;

CREATE OR REPLACE FUNCTION note_tailor_failure(p_job_id bigint) RETURNS int
LANGUAGE sql AS $$
  UPDATE jobs SET tailor_attempts = tailor_attempts + 1, tailoring_claimed_at = NULL
   WHERE id = p_job_id RETURNING tailor_attempts
$$;

-- Applications created since the start of the day in the budget timezone (for the daily limit).
CREATE OR REPLACE FUNCTION applications_today() RETURNS int
LANGUAGE sql STABLE AS $$
  SELECT count(*)::int FROM applications a, budget_settings b
   WHERE b.id = 1
     AND a.created_at >= (date_trunc('day', now() AT TIME ZONE b.timezone)) AT TIME ZONE b.timezone
$$;

-- Record a finished application and move the job on. In 'review' mode the job waits as 'tailored';
-- in 'auto' mode it is approved straight away. Re-running for the same job replaces the draft but
-- never touches an application the user has already acted on.
CREATE OR REPLACE FUNCTION save_application(
  p_job_id bigint, p_variant text, p_files_dir text, p_resume_path text, p_cover_path text,
  p_patch jsonb, p_requirements jsonb, p_audit jsonb, p_model text, p_cost numeric,
  p_needs_attention boolean, p_attention_reason text, p_approval text
) RETURNS text LANGUAGE plpgsql AS $$
DECLARE v_status text;
BEGIN
  v_status := CASE WHEN p_approval = 'auto' AND NOT p_needs_attention THEN 'approved' ELSE 'drafted' END;
  INSERT INTO applications (job_id, status, variant, files_dir, resume_path, cover_letter_path, tailor_patch,
                            requirements, audit, model, cost_usd, needs_attention, attention_reason)
  VALUES (p_job_id, v_status, p_variant, p_files_dir, p_resume_path, p_cover_path, p_patch,
          coalesce(p_requirements, '[]'::jsonb), coalesce(p_audit, '{}'::jsonb), p_model, coalesce(p_cost, 0),
          p_needs_attention, p_attention_reason)
  ON CONFLICT (job_id) DO UPDATE SET
    status = EXCLUDED.status, variant = EXCLUDED.variant, files_dir = EXCLUDED.files_dir,
    resume_path = EXCLUDED.resume_path, cover_letter_path = EXCLUDED.cover_letter_path,
    tailor_patch = EXCLUDED.tailor_patch, requirements = EXCLUDED.requirements, audit = EXCLUDED.audit,
    model = EXCLUDED.model, cost_usd = applications.cost_usd + EXCLUDED.cost_usd,
    needs_attention = EXCLUDED.needs_attention, attention_reason = EXCLUDED.attention_reason,
    reviewed_at = NULL, review_note = NULL, updated_at = now()
  WHERE applications.status IN ('drafted', 'approved', 'skipped');

  UPDATE jobs SET status = CASE WHEN v_status = 'approved' THEN 'approved' ELSE 'tailored' END,
                  tailoring_claimed_at = NULL
   WHERE id = p_job_id AND status IN ('shortlisted', 'tailored', 'approved', 'skipped');
  RETURN v_status;
END;
$$;

-- Approve / skip / mark applied. Each only moves forward from a state where it makes sense.
CREATE OR REPLACE FUNCTION review_application(p_job_id bigint, p_action text, p_note text DEFAULT NULL) RETURNS text
LANGUAGE plpgsql AS $$
DECLARE v_new text; v_cur text;
BEGIN
  SELECT status INTO v_cur FROM applications WHERE job_id = p_job_id;
  IF v_cur IS NULL THEN RAISE EXCEPTION 'no application for job %', p_job_id; END IF;
  v_new := CASE p_action
             WHEN 'approve' THEN CASE WHEN v_cur IN ('drafted', 'skipped') THEN 'approved' END
             WHEN 'skip'    THEN CASE WHEN v_cur IN ('drafted', 'approved') THEN 'skipped' END
             WHEN 'applied' THEN CASE WHEN v_cur IN ('drafted', 'approved') THEN 'applied' END
             ELSE NULL END;
  IF p_action NOT IN ('approve', 'skip', 'applied') THEN RAISE EXCEPTION 'unknown action %', p_action; END IF;
  IF v_new IS NULL THEN RAISE EXCEPTION 'cannot % an application that is %', p_action, v_cur; END IF;
  UPDATE applications SET status = v_new, reviewed_at = now(), review_note = coalesce(p_note, review_note),
         applied_at = CASE WHEN v_new = 'applied' THEN now() ELSE applied_at END, updated_at = now()
   WHERE job_id = p_job_id;
  UPDATE jobs SET status = v_new WHERE id = p_job_id AND status IN ('shortlisted', 'tailored', 'approved', 'skipped', 'applied');
  RETURN v_new;
END;
$$;

-- ---------------------------------------------------------------- Telegram polling
CREATE TABLE telegram_state (
  id            int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  update_offset bigint NOT NULL DEFAULT 0,
  updated_at    timestamptz NOT NULL DEFAULT now()
);
INSERT INTO telegram_state DEFAULT VALUES;

-- The review queue: drafted applications that need a decision, newest first.
CREATE OR REPLACE VIEW v_review_queue AS
SELECT a.job_id, j.title, j.company, j.location, j.score, j.verdict, a.variant, a.needs_attention,
       a.attention_reason, a.files_dir, a.created_at
  FROM applications a JOIN jobs j ON j.id = a.job_id
 WHERE a.status = 'drafted';
