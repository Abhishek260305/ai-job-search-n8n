-- 013: company job boards and remote feeds (Phase 7).
--
--  1. Settings: which company boards to read (search_profile.boards, keyed by board type) and the title words
--     a board posting must contain to be kept (board_title_words); how many boards one collect run visits
--     (schedule_settings.board_batch), so a long list is rotated through instead of fetched all at once.
--  2. fetch_state: one row per (source, key) remembering when it was last fetched, whether that worked, and how
--     many times in a row it failed. Free feeds ask for polite intervals; a feed that keeps failing is surfaced
--     to the doctor instead of being retried silently forever.
--  3. claim_fetch: the politeness gate. One statement, so two overlapping runs cannot both fetch the same feed.
--  4. record_fetch: the outcome of a fetch (success resets the failure count; failure counts up).
--  Nothing here changes existing functions: schedule_status() and upsert_job() are untouched.

-- ---------------------------------------------------------------- 1. settings
ALTER TABLE search_profile
  ADD COLUMN boards             jsonb  NOT NULL DEFAULT '{}',    -- e.g. {"greenhouse": ["acme"], "lever": ["newco"]}
  ADD COLUMN board_title_words  text[] NOT NULL DEFAULT '{}';    -- a board posting's title must contain one (if non-empty)

ALTER TABLE schedule_settings
  ADD COLUMN board_batch int NOT NULL DEFAULT 30 CHECK (board_batch BETWEEN 1 AND 200);

-- ---------------------------------------------------------------- 2. fetch_state
CREATE TABLE fetch_state (
  source          text NOT NULL,                   -- 'remotive', 'greenhouse', ...
  key             text NOT NULL DEFAULT '',        -- the board token, or '' for a feed with no sub-key
  last_fetched_at timestamptz,                     -- when it was last CLAIMED (attempted)
  last_ok_at      timestamptz,                     -- when it last succeeded
  last_count      int,                             -- postings returned by the last success
  failures        int NOT NULL DEFAULT 0,          -- consecutive failures
  last_error      text,                            -- the latest failure, cut to 300 characters
  PRIMARY KEY (source, key)
);

-- ---------------------------------------------------------------- 3. claim_fetch
-- True (and the clock restarts) only when this (source, key) was never fetched or the interval has passed.
-- The whole check-and-set is one INSERT .. ON CONFLICT DO UPDATE .. WHERE, so two callers racing for the same
-- feed cannot both get true: the loser sees the winner's fresh last_fetched_at and no row comes back.
CREATE FUNCTION claim_fetch(p_source text, p_key text, p_min_interval interval) RETURNS boolean
LANGUAGE plpgsql AS $$
DECLARE won boolean;
BEGIN
  INSERT INTO fetch_state AS f (source, key, last_fetched_at)
  VALUES (p_source, p_key, now())
  ON CONFLICT (source, key) DO UPDATE SET last_fetched_at = now()
   WHERE f.last_fetched_at IS NULL OR f.last_fetched_at <= now() - p_min_interval
  RETURNING true INTO won;
  RETURN coalesce(won, false);
END;
$$;

-- ---------------------------------------------------------------- 4. record_fetch
-- Success: failures back to 0, remember when and how many. Failure: count it and keep the error (cut to 300
-- characters); the previous success is kept, so "last worked 3 days ago, failing since" stays readable.
-- last_fetched_at is set only when this INSERT creates a NEW row: an existing row keeps the time claim_fetch gave it,
-- because callers always claim first and record afterwards (a recorded outcome must not restart the politeness clock).
CREATE FUNCTION record_fetch(p_source text, p_key text, p_ok boolean, p_count int, p_error text) RETURNS void
LANGUAGE sql AS $$
  INSERT INTO fetch_state AS f (source, key, last_fetched_at, last_ok_at, last_count, failures, last_error)
  VALUES (p_source, p_key, now(),
          CASE WHEN p_ok THEN now() END, CASE WHEN p_ok THEN p_count END,
          CASE WHEN p_ok THEN 0 ELSE 1 END, CASE WHEN p_ok THEN NULL ELSE left(p_error, 300) END)
  ON CONFLICT (source, key) DO UPDATE SET
    last_ok_at = CASE WHEN p_ok THEN now() ELSE f.last_ok_at END,
    last_count = CASE WHEN p_ok THEN p_count ELSE f.last_count END,
    failures   = CASE WHEN p_ok THEN 0 ELSE f.failures + 1 END,
    last_error = CASE WHEN p_ok THEN NULL ELSE left(p_error, 300) END
$$;
