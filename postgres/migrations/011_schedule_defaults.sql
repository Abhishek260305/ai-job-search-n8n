-- 011: two refinements to the schedules from 009.
--
--  * The digest may run every N hours as well as daily at a time (for example every 6 hours, right after
--    each search). A schedule is still exactly one of the two.
--  * Scoring defaults to 25 jobs per run (it was 10): one collect run can add a couple of hundred jobs and
--    scoring costs about $0.0003 each, so 10 per 30 minutes left a backlog of many hours. Only an untouched
--    default is changed; a value you set yourself is kept.

DO $$
DECLARE c record;
BEGIN
  FOR c IN SELECT conname FROM pg_constraint
            WHERE conrelid = 'schedule_settings'::regclass AND contype = 'c'
              AND pg_get_constraintdef(oid) ~ 'digest.*(at_time|every_minutes) IS NOT NULL'
  LOOP
    EXECUTE format('ALTER TABLE schedule_settings DROP CONSTRAINT %I', c.conname);
  END LOOP;
END $$;

ALTER TABLE schedule_settings
  ADD CONSTRAINT schedule_digest_interval_or_daily CHECK (name <> 'digest' OR every_minutes IS NOT NULL OR at_time IS NOT NULL),
  ADD CONSTRAINT schedule_others_are_intervals     CHECK (name = 'digest' OR every_minutes IS NOT NULL),
  ADD CONSTRAINT schedule_digest_min_hour          CHECK (name <> 'digest' OR every_minutes IS NULL OR every_minutes >= 60);

UPDATE schedule_settings SET batch = 25 WHERE name = 'score' AND batch = 10;
