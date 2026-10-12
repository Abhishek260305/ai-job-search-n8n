#!/bin/sh
# Give the dashboard's database role (migration 014) its password from .env, then exit. The dashboard-db-init
# service runs this as the admin user on every `docker compose up`, so a changed DASHBOARD_DB_PASSWORD applies.
set -eu

: "${DASHBOARD_DB_PASSWORD:=}"
if [ "${#DASHBOARD_DB_PASSWORD}" -lt 16 ]; then
  echo "dashboard-db-init: DASHBOARD_DB_PASSWORD must be set in .env (16+ characters): scripts/init-env.sh --add-dashboard" >&2
  exit 1
fi

# psql takes the password from the environment (\getenv) and quotes it itself (:'pw'), so it never appears on a
# command line, where any user of the host could read it with ps.
if ! psql -v ON_ERROR_STOP=1 -X -q <<'SQL'
\getenv pw DASHBOARD_DB_PASSWORD
ALTER ROLE jobsearch_dashboard LOGIN PASSWORD :'pw';
SQL
then
  echo "dashboard-db-init: could not set the password. If the role does not exist, apply migration 014 first:" \
       "scripts/jobsearch migrate, then docker compose up -d" >&2
  exit 1
fi
echo "dashboard-db-init: the jobsearch_dashboard role can log in"
