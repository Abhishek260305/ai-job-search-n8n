-- 005: spend logging and hard budget caps for paid API usage.
-- Layer 1 (this file): every call's tokens and cost are logged, and a monthly + daily cap is
-- checked BEFORE a batch is claimed (worst-case cost reserved up front).
-- Layer 2 (outside this repo): a spend limit on the provider's own console.

-- Prices per million tokens. Source: platform.claude.com/docs/en/about-claude/pricing (checked
-- 2026-10-11). Prices change: keep this table current (see docs/budget.md). Claude Haiku 5.5 charges
-- higher rates for prompts over 100,000 tokens; this pipeline's prompts are about 3,000 tokens.
CREATE TABLE model_pricing (
  model                 text PRIMARY KEY,
  input_per_mtok        numeric NOT NULL,
  output_per_mtok       numeric NOT NULL,
  cache_write_per_mtok  numeric NOT NULL,   -- 5-minute cache write
  cache_read_per_mtok   numeric NOT NULL,
  source                text,
  verified_on           date
);
INSERT INTO model_pricing VALUES
  ('claude-haiku-5-5',  0.10, 0.50,  0.125, 0.01, 'platform.claude.com/docs/en/about-claude/pricing', '2026-10-11'),
  ('claude-sonnet-5-5', 2.00, 10.00, 2.50,  0.10, 'platform.claude.com/docs/en/about-claude/pricing', '2026-10-11'),
  ('claude-opus-5-5',   4.00, 20.00, 5.00,  0.20, 'platform.claude.com/docs/en/about-claude/pricing', '2026-10-11');

CREATE TABLE llm_usage (
  id                bigserial PRIMARY KEY,
  at                timestamptz NOT NULL DEFAULT now(),
  provider          text NOT NULL DEFAULT 'anthropic',     -- anthropic | apify
  purpose           text NOT NULL,                         -- score | tailor | search | ...
  model             text,
  job_id            bigint,                                -- no FK: usage history outlives jobs
  input_tokens      int NOT NULL DEFAULT 0,
  output_tokens     int NOT NULL DEFAULT 0,
  cache_write_tokens int NOT NULL DEFAULT 0,
  cache_read_tokens int NOT NULL DEFAULT 0,
  cost_usd          numeric(12, 6) NOT NULL DEFAULT 0,
  ok                boolean NOT NULL DEFAULT true,
  note              text
);
CREATE INDEX llm_usage_at_idx ON llm_usage (at);

-- One row of settings. Caps apply to provider = 'anthropic' spend.
CREATE TABLE budget_settings (
  id               int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  monthly_cap_usd  numeric NOT NULL DEFAULT 10 CHECK (monthly_cap_usd >= 0),
  daily_cap_usd    numeric NOT NULL DEFAULT 1.5 CHECK (daily_cap_usd >= 0),
  warn_pct         int NOT NULL DEFAULT 80 CHECK (warn_pct BETWEEN 1 AND 100),
  timezone         text NOT NULL DEFAULT 'Asia/Kolkata',   -- month and day boundaries
  updated_at       timestamptz NOT NULL DEFAULT now()
);
INSERT INTO budget_settings DEFAULT VALUES;

-- Cost of one call. An unknown model is priced at the most expensive known rates, so a typo or a
-- new model can never make spend look cheaper than it is.
CREATE OR REPLACE FUNCTION usage_cost(
  p_model text, p_in int, p_out int, p_cache_write int, p_cache_read int
) RETURNS numeric LANGUAGE sql STABLE AS $$
  SELECT (coalesce(p_in, 0)          * p.input_per_mtok
        + coalesce(p_out, 0)         * p.output_per_mtok
        + coalesce(p_cache_write, 0) * p.cache_write_per_mtok
        + coalesce(p_cache_read, 0)  * p.cache_read_per_mtok) / 1000000.0
    FROM (
      SELECT input_per_mtok, output_per_mtok, cache_write_per_mtok, cache_read_per_mtok
        FROM model_pricing WHERE model = p_model
      UNION ALL
      SELECT max(input_per_mtok), max(output_per_mtok), max(cache_write_per_mtok), max(cache_read_per_mtok)
        FROM model_pricing WHERE NOT EXISTS (SELECT 1 FROM model_pricing WHERE model = p_model)
    ) p
   LIMIT 1
$$;

-- Log one call. p_cost_override is for providers without token pricing (e.g. an Apify estimate).
CREATE OR REPLACE FUNCTION record_usage(
  p_provider text, p_purpose text, p_model text, p_job_id bigint,
  p_in int DEFAULT 0, p_out int DEFAULT 0, p_cache_write int DEFAULT 0, p_cache_read int DEFAULT 0,
  p_ok boolean DEFAULT true, p_note text DEFAULT NULL, p_cost_override numeric DEFAULT NULL
) RETURNS numeric LANGUAGE plpgsql AS $$
DECLARE v_cost numeric;
BEGIN
  v_cost := coalesce(p_cost_override, usage_cost(p_model, p_in, p_out, p_cache_write, p_cache_read));
  INSERT INTO llm_usage (provider, purpose, model, job_id, input_tokens, output_tokens,
                         cache_write_tokens, cache_read_tokens, cost_usd, ok, note)
  VALUES (p_provider, p_purpose, p_model, p_job_id, coalesce(p_in, 0), coalesce(p_out, 0),
          coalesce(p_cache_write, 0), coalesce(p_cache_read, 0), v_cost, p_ok,
          CASE WHEN p_provider = 'anthropic' AND p_cost_override IS NULL
                    AND NOT EXISTS (SELECT 1 FROM model_pricing WHERE model = p_model)
               THEN coalesce(p_note || '; ', '') || 'unknown model: priced at the most expensive known rates'
               ELSE p_note END);
  RETURN v_cost;
END;
$$;

-- Where spend stands against the caps, in the configured timezone. Only Anthropic spend counts.
CREATE OR REPLACE FUNCTION budget_status() RETURNS TABLE (
  month_spent numeric, day_spent numeric, monthly_cap numeric, daily_cap numeric,
  month_remaining numeric, day_remaining numeric, warn boolean, blocked boolean,
  blocked_reason text, warn_pct int, month_start timestamptz
) LANGUAGE sql STABLE AS $$
  WITH s AS (SELECT * FROM budget_settings WHERE id = 1),
  w AS (SELECT s.*,
               (date_trunc('month', now() AT TIME ZONE s.timezone)) AT TIME ZONE s.timezone AS m0,
               (date_trunc('day',   now() AT TIME ZONE s.timezone)) AT TIME ZONE s.timezone AS d0 FROM s),
  t AS (SELECT w.*,
               coalesce((SELECT sum(cost_usd) FROM llm_usage u WHERE u.provider = 'anthropic' AND u.at >= w.m0), 0) AS ms,
               coalesce((SELECT sum(cost_usd) FROM llm_usage u WHERE u.provider = 'anthropic' AND u.at >= w.d0), 0) AS ds FROM w)
  SELECT ms, ds, monthly_cap_usd, daily_cap_usd,
         greatest(monthly_cap_usd - ms, 0), greatest(daily_cap_usd - ds, 0),
         ms >= monthly_cap_usd * warn_pct / 100.0,
         ms >= monthly_cap_usd OR ds >= daily_cap_usd,
         CASE WHEN ms >= monthly_cap_usd THEN 'monthly cap reached'
              WHEN ds >= daily_cap_usd THEN 'daily cap reached' END,
         warn_pct, m0
    FROM t
$$;

CREATE OR REPLACE VIEW v_spend_month AS
SELECT date_trunc('day', u.at AT TIME ZONE b.timezone)::date AS day, u.provider, u.purpose, u.model,
       count(*) AS calls, sum(u.input_tokens) AS input_tokens, sum(u.output_tokens) AS output_tokens,
       sum(u.cache_read_tokens) AS cache_read_tokens, round(sum(u.cost_usd), 4) AS cost_usd
  FROM llm_usage u, budget_settings b
 WHERE u.at >= (date_trunc('month', now() AT TIME ZONE b.timezone)) AT TIME ZONE b.timezone
 GROUP BY 1, 2, 3, 4;
