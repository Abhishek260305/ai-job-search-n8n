-- 027: the original tailoring writer (v1) is retired. Tailoring v2 is the only one: the setting stays (it names the writer, and a
-- later one can be added), but it can only say v2. A row that still says v1 is moved to v2, and the default is v2.
-- (Order of release: the new scraper image first, which already runs an old "v1" value as v2, then this migration.)

ALTER TABLE tailor_settings DROP CONSTRAINT IF EXISTS tailor_settings_engine_check;
UPDATE tailor_settings SET engine = 'v2' WHERE engine <> 'v2';
ALTER TABLE tailor_settings ALTER COLUMN engine SET DEFAULT 'v2';
ALTER TABLE tailor_settings ADD CONSTRAINT tailor_settings_engine_check CHECK (engine = 'v2');
