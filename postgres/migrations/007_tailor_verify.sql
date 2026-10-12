-- 007: a second, semantic honesty layer for tailoring: a cheap model fact-checks the generated text
-- against the resume (see tailor.py verify_prompt). On by default.
ALTER TABLE tailor_settings
  ADD COLUMN verify       boolean NOT NULL DEFAULT true,
  ADD COLUMN verify_model text    NOT NULL DEFAULT 'claude-haiku-5-5';
