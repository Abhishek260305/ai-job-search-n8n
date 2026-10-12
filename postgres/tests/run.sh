#!/bin/sh
# Build a throwaway database two ways and run the SQL tests against it.
#   docker compose exec -T postgres sh /tests/run.sh
#
#  A) fresh install:  schema.sql -> migrate.sh               (what a new user gets)
#  B) existing install: schema.sql only (baseline, with a row) -> migrate.sh later
# The real databases are never touched.
set -e
export MIGRATIONS_DIR="${MIGRATIONS_DIR:-/migrations}"
admin() { psql -v ON_ERROR_STOP=1 -X -q -U "$POSTGRES_USER" -d postgres "$@"; }
cleanup() { admin -c "DROP DATABASE IF EXISTS jobsearch_test_a" -c "DROP DATABASE IF EXISTS jobsearch_test_b" >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup

echo "== A) fresh install =="
admin -c "CREATE DATABASE jobsearch_test_a"
psql -v ON_ERROR_STOP=1 -X -q -U "$POSTGRES_USER" -d jobsearch_test_a -f /schema/schema.sql
sh /migrate.sh jobsearch_test_a
echo "-- migrate twice is a no-op:"; sh /migrate.sh jobsearch_test_a
psql -v ON_ERROR_STOP=1 -X -q -U "$POSTGRES_USER" -d jobsearch_test_a -f /tests/test_migrations.sql

echo "== B) existing baseline database with data =="
admin -c "CREATE DATABASE jobsearch_test_b"
psql -v ON_ERROR_STOP=1 -X -q -U "$POSTGRES_USER" -d jobsearch_test_b -f /schema/schema.sql
psql -v ON_ERROR_STOP=1 -X -q -U "$POSTGRES_USER" -d jobsearch_test_b -c \
  "SELECT upsert_job('indeed','old-1','u',NULL,'Legacy Role','Legacy Corp','Pune',false,now(),'old row',NULL,NULL,NULL,'{}'::jsonb)"
sh /migrate.sh jobsearch_test_b
rows=$(psql -X -At -U "$POSTGRES_USER" -d jobsearch_test_b -c "SELECT count(*) FROM jobs WHERE fingerprint = job_fingerprint(company, title, location)")
[ "$rows" = "1" ] || { echo "existing row was not re-fingerprinted"; exit 1; }
echo "existing row survived and was re-fingerprinted"
versions=$(psql -X -At -U "$POSTGRES_USER" -d jobsearch_test_b -c "SELECT string_agg(version, ',' ORDER BY version) FROM schema_migrations")
echo "versions: $versions"
[ "$versions" = "000_baseline,001_safety_scoring,002_record_score,003_pipeline,004_dedupe_locations,005_budget,006_tailoring,007_tailor_verify,008_features,009_schedules,010_schedule_status_fix,011_schedule_defaults,012_runaway_guards,013_boards,014_dashboard,015_reshortlist,016_dashboard_ops,017_profile_edited_by" ] || { echo "unexpected migration records"; exit 1; }
echo "ALL MIGRATION TESTS PASSED"
