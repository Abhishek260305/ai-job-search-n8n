-- 010: a schedule that has never run is due at the next tick, so schedule_status() reports it as due now
-- (it used to report "last run + interval" from the moment it was first looked at). The claiming logic was already right.
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
   ORDER BY CASE s.name WHEN 'collect' THEN 1 WHEN 'score' THEN 2 WHEN 'tailor' THEN 3 ELSE 4 END;
$$;
