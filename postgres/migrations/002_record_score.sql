-- 002: one call to store a scoring result (the JSON produced by scraper/scoring.py finalize()).
-- n8n usage:  SELECT record_score($1::bigint, $2::jsonb)

CREATE OR REPLACE FUNCTION record_score(p_job_id bigint, p jsonb) RETURNS text
LANGUAGE plpgsql AS $$
DECLARE
  v_status text;
BEGIN
  UPDATE jobs SET
    score         = (p->>'score')::int,
    verdict       = p->>'verdict',
    scores        = p->'scores',
    gates         = p->'gates',
    strengths     = coalesce(p->'strengths', '[]'::jsonb),
    gaps          = coalesce(p->'gaps', '[]'::jsonb),
    score_reason  = p->>'score_reason',
    scored_at     = now(),
    deadline      = coalesce(nullif(p->>'deadline', '')::date, deadline),
    posting_language = coalesce(p->>'posting_language', posting_language),
    yoe_min       = coalesce((p->>'yoe_min')::int, yoe_min),
    yoe_max       = coalesce((p->>'yoe_max')::int, yoe_max),
    -- only move jobs that are still in the triage stages; never touch a job already
    -- tailored/approved/applied, blocked as a scam, or expired.
    status        = CASE WHEN status IN ('new', 'scored', 'shortlisted', 'filtered_out')
                         THEN p->>'next_status' ELSE status END,
    filter_reason = CASE WHEN status IN ('new', 'scored', 'shortlisted', 'filtered_out')
                         THEN p->>'filter_reason' ELSE filter_reason END
  WHERE id = p_job_id
  RETURNING status INTO v_status;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'record_score: job % not found', p_job_id;
  END IF;
  RETURN v_status;
END;
$$;
