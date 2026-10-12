#!/bin/sh
# Run the Python unit tests (scraper + CLI). SQL tests run inside the postgres container:
#   docker compose exec -T postgres sh /tests/run.sh
set -e
cd "$(dirname "$0")/.."
PY="${PYTHON:-python3}"
echo "== scraper =="; (cd scraper && "$PY" -m pytest -q -p no:cacheprovider)
echo "== cli ==";     (cd cli     && "$PY" -m pytest -q -p no:cacheprovider)
