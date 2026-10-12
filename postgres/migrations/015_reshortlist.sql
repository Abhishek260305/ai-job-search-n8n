-- 015: jobs the scorer left at 'scored' that are at or above today's threshold.
--
-- The scorer decides "shortlisted or just scored" once, with the threshold of the moment. Lower the threshold
-- afterwards and every job already scored between the new and the old value stays 'scored': it is never sent in
-- the digest or tailored, although it now counts as a match. v_reshortlist lists those jobs and reshortlist()
-- promotes them (scripts/jobsearch reshortlist [--apply]).
--
-- Only plain 'scored' jobs: a job a gate excluded is 'filtered_out' and stays out whatever its score; scam-blocked,
-- expired and acted-on jobs (an application row exists) are left alone. Nothing is ever demoted.
CREATE VIEW v_reshortlist AS
SELECT j.id, j.score, j.verdict, j.title, j.company
FROM jobs j
WHERE j.status = 'scored'
  AND j.score >= (SELECT score_threshold FROM search_profile WHERE name = 'default')
  AND coalesce(j.scam_risk, 'low') <> 'high'
  AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.job_id = j.id);

CREATE FUNCTION reshortlist() RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE n integer;
BEGIN
  UPDATE jobs SET status = 'shortlisted' WHERE id IN (SELECT id FROM v_reshortlist) AND status = 'scored';
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END;
$$;
