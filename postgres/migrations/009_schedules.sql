-- 009: schedules you can change, plus two tailoring options.
--
-- Until now each workflow carried its own fixed schedule inside n8n. Now one small scheduler workflow ticks
-- every few minutes, asks claim_due_schedules() what is due, and starts the matching workflow. Everything
-- about *when* and *how much* lives here, so it changes with one command and no n8n restart.
--
--   collect  every N minutes   searches the next `batch` (title x location x source) combinations
--   score    every N minutes   scores up to `batch` new jobs with `model`
--   tailor   every N minutes   tailors up to `batch` shortlisted jobs
--   digest   daily at at_time  lists up to `batch` new matches on Telegram
--
-- The timezone for the daily time (and for the spending-cap days) is budget_settings.timezone.

CREATE TABLE schedule_settings (
  name          text PRIMARY KEY CHECK (name IN ('collect', 'score', 'tailor', 'digest')),
  enabled       boolean NOT NULL DEFAULT true,
  every_minutes int  CHECK (every_minutes BETWEEN 5 AND 10080),   -- interval schedules (5 minutes to 7 days)
  at_time       time,                                              -- daily schedules, in the configured timezone
  batch         int  NOT NULL CHECK (batch BETWEEN 1 AND 50),      -- how much work one run does
  model         text CHECK (model ~ '^[A-Za-z0-9._-]{3,80}$'),     -- the scoring model (score only)
  run_now       boolean NOT NULL DEFAULT false,                    -- "run it at the next tick, whatever the schedule says"
  last_run_at   timestamptz,
  updated_at    timestamptz NOT NULL DEFAULT now(),
  CHECK ((every_minutes IS NULL) <> (at_time IS NULL)),            -- a schedule is either an interval or a daily time
  CHECK (name = 'digest' OR every_minutes IS NOT NULL),
  CHECK (name <> 'digest' OR at_time IS NOT NULL),
  CHECK (name <> 'collect' OR every_minutes >= 15),                -- be gentle with the job boards
  CHECK (name = 'score' OR model IS NULL)
);

-- Defaults: collect every 6 hours; with 18 combinations per run that covers about 72 a day, which is
-- the same coverage as 6 every 2 hours used to give.
INSERT INTO schedule_settings (name, every_minutes, at_time, batch, model) VALUES
  ('collect', 360,  NULL,    18, NULL),
  ('score',   30,   NULL,    10, 'claude-haiku-5-5'),
  ('tailor',  15,   NULL,    3,  NULL),
  ('digest',  NULL, '08:00', 10, NULL);

-- Claim everything that is due, atomically: a run is recorded as started the moment it is claimed, so two
-- overlapping ticks can never start the same job twice. `p_skip` names schedules to leave alone because their
-- feature is switched off; they are not claimed, so they fire promptly when switched back on.
-- A schedule with run_now set is claimed once at the next tick even if it is disabled (an explicit request).
-- A daily schedule whose time has passed fires once that day, even if the server was down at the exact minute.
CREATE OR REPLACE FUNCTION claim_due_schedules(p_skip text[] DEFAULT '{}') RETURNS TABLE (name text, batch int, model text)
LANGUAGE sql AS $$
  WITH tz AS (SELECT timezone AS z FROM budget_settings WHERE id = 1),
       nowl AS (SELECT (now() AT TIME ZONE tz.z) AS t, tz.z FROM tz)
  UPDATE schedule_settings s SET last_run_at = now(), run_now = false
    FROM nowl
   WHERE NOT (s.name = ANY (p_skip))
     AND ( s.run_now
        OR ( s.enabled AND (
               (s.every_minutes IS NOT NULL
                  AND (s.last_run_at IS NULL OR now() >= s.last_run_at + s.every_minutes * interval '1 minute'))
            OR (s.at_time IS NOT NULL
                  AND nowl.t::time >= s.at_time
                  AND (s.last_run_at IS NULL OR (s.last_run_at AT TIME ZONE nowl.z)::date < nowl.t::date)) )))
  RETURNING s.name, s.batch, s.model;
$$;

-- What the CLI shows: each schedule with when it last ran and when it runs next.
CREATE OR REPLACE FUNCTION schedule_status() RETURNS TABLE (
  name text, enabled boolean, every_minutes int, at_time time, batch int, model text, run_now boolean,
  last_run_at timestamptz, next_run_at timestamptz, timezone text)
LANGUAGE sql STABLE AS $$
  WITH tz AS (SELECT b.timezone AS z FROM budget_settings b WHERE b.id = 1)
  SELECT s.name, s.enabled, s.every_minutes, s.at_time, s.batch, s.model, s.run_now, s.last_run_at,
         CASE
           WHEN NOT s.enabled THEN NULL
           WHEN s.every_minutes IS NOT NULL THEN coalesce(s.last_run_at, now()) + s.every_minutes * interval '1 minute'
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

-- ---------------------------------------------------------------- tailoring options
ALTER TABLE tailor_settings
  ADD COLUMN paper       text NOT NULL DEFAULT 'a4' CHECK (paper IN ('a4', 'letter')),
  ADD COLUMN style_notes text CHECK (style_notes IS NULL OR length(style_notes) <= 400);
