-- 026: tailoring v2 (docs/tailoring.md, "Comparing the engines, and tailoring v2"): settings and a small cache.
--
--  * tailor_settings.engine           v1 (what has always run, the default) or v2 (the gap policy, proof points, skill levels,
--                                     ownership checks, a reviewer pass, an evidence-style letter). Switch with
--                                     `scripts/jobsearch tailor-settings set --engine v2` after comparing with `tailor-compare`.
--  * tailor_settings.gap_policy       omit (the letter and summary never state a gap) or bridge ("My closest work is ...")
--  * tailor_settings.cover_style      evidence (an opening, achievement lines, what you would work on first) or classic (3 paragraphs)
--  * tailor_settings.tone             formal, direct or conversational
--  * tailor_settings.reviewer         one more cheap call that proposes edits; the audit decides which are kept
--  * tailor_settings.reviewer_model   the model for it (default Claude Haiku)
--  * tailor_settings.company_research up to three plain facts from the employer's own website, cached below
--  * company_notes                    those facts, per company, 30 days. No personal data, no email text. Not visible to the dashboard role.
-- The v2 settings only matter while engine = v2; with the default engine = v1 nothing changes.

ALTER TABLE tailor_settings
  ADD COLUMN engine           text    NOT NULL DEFAULT 'v1'                 CHECK (engine IN ('v1', 'v2')),
  ADD COLUMN gap_policy       text    NOT NULL DEFAULT 'omit'               CHECK (gap_policy IN ('omit', 'bridge')),
  ADD COLUMN cover_style      text    NOT NULL DEFAULT 'evidence'           CHECK (cover_style IN ('evidence', 'classic')),
  ADD COLUMN tone             text    NOT NULL DEFAULT 'direct'             CHECK (tone IN ('formal', 'direct', 'conversational')),
  ADD COLUMN reviewer         boolean NOT NULL DEFAULT true,
  ADD COLUMN reviewer_model   text    NOT NULL DEFAULT 'claude-haiku-5-5'   CHECK (reviewer_model ~ '^[A-Za-z0-9._-]{3,80}$'),
  ADD COLUMN company_research boolean NOT NULL DEFAULT true;

CREATE TABLE company_notes (
  company_key text        PRIMARY KEY CHECK (length(company_key) BETWEEN 1 AND 80),
  facts       jsonb       NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(facts) = 'array' AND jsonb_array_length(facts) <= 3),
  source_url  text        CHECK (source_url IS NULL OR length(source_url) <= 300),
  fetched_at  timestamptz NOT NULL DEFAULT now()
);
REVOKE ALL ON company_notes FROM PUBLIC;
