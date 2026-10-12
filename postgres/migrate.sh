#!/bin/sh
# Apply pending SQL migrations to a database inside the postgres container.
#
#   docker compose exec -T postgres sh /migrate.sh [database]     (default: jobsearch)
#
# Migrations live in postgres/migrations/NNN_name.sql and run once each, in filename order,
# every one inside a single transaction together with its schema_migrations record.
# Databases created from schema.sql before migrations existed are marked as '000_baseline'.
set -e
DB="${1:-jobsearch}"
DIR="${MIGRATIONS_DIR:-/migrations}"

q() { psql -v ON_ERROR_STOP=1 -X -q -U "$POSTGRES_USER" -d "$DB" "$@"; }

q -c "CREATE TABLE IF NOT EXISTS schema_migrations (
        version text PRIMARY KEY,
        applied_at timestamptz NOT NULL DEFAULT now())"

if [ "$(q -At -c "SELECT to_regclass('public.jobs') IS NOT NULL")" = "t" ]; then
  q -c "INSERT INTO schema_migrations (version) VALUES ('000_baseline') ON CONFLICT DO NOTHING"
fi

applied=0
for f in "$DIR"/*.sql; do
  [ -e "$f" ] || continue
  v=$(basename "$f" .sql)
  if [ "$(q -At -c "SELECT 1 FROM schema_migrations WHERE version = '$v'")" = "1" ]; then
    continue
  fi
  echo "applying $v"
  q -1 -f "$f" -c "INSERT INTO schema_migrations (version) VALUES ('$v')"
  applied=$((applied + 1))
done
echo "$applied migration(s) applied to $DB"
