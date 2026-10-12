-- 017: who last wrote the search profile.
--
-- The search profile (terms, locations, sources, company boards, filters) is written from two places: the CLI
-- (`search-profile apply`, `sources`), which copies config/search.json on your computer into this table, and, from
-- dashboard version 2c, the dashboard, which writes it here directly. The file cannot be written from the server, so
-- after a dashboard edit the file is behind. edited_by says which side wrote last, so the CLI can refuse to overwrite
-- a dashboard edit with an older file (`search-profile pull` brings the file up to date first, `--force` discards the
-- dashboard edit), and the doctor can say the file is behind.
ALTER TABLE search_profile
  ADD COLUMN edited_by text NOT NULL DEFAULT 'cli' CHECK (edited_by IN ('cli', 'dashboard'));
