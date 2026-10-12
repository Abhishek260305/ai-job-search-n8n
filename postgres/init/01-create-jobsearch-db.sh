#!/bin/sh
# Runs once, on first start of an empty data volume.
set -e
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres -c "CREATE DATABASE jobsearch"
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d jobsearch -f /schema/schema.sql
# schema.sql is the baseline; bring a fresh database up to date with all migrations.
sh /migrate.sh jobsearch
