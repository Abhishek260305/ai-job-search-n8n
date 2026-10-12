#!/bin/sh
# Create .env from .env.example with freshly generated secrets.
# Refuses to overwrite an existing .env (the encryption key must stay stable).
#
#   scripts/init-env.sh                   a new .env. The web dashboard's values are generated too; it stays off
#                                         until COMPOSE_PROFILES=dashboard.
#   scripts/init-env.sh --add-dashboard   add the web dashboard to an existing .env and turn it on. Only keys that
#                                         are missing or empty are filled in; every other line stays as it is.
#
# Prints key names, never values.
set -e
cd "$(dirname "$0")/.."
umask 077
trap 'rm -f .env.tmp .env.new' EXIT
command -v openssl > /dev/null || { echo "openssl is needed to generate the secrets." >&2; exit 1; }

# put KEY VALUE FILE: replace the first KEY= line, or append one. The value goes through the environment, not
# the command line, so it never shows up in ps.
put() {
  if grep -q "^$1=" "$3"; then
    K="$1" V="$2" awk '!done && index($0, ENVIRON["K"] "=") == 1 { print ENVIRON["K"] "=" ENVIRON["V"]; done = 1; next }
                       { print }' "$3" > .env.tmp
    mv .env.tmp "$3"
  else
    printf '%s=%s\n' "$1" "$2" >> "$3"
  fi
}

# URL-safe base64, so the password needs no quoting anywhere.
new_value() {
  case $1 in
    DASHBOARD_PASSWORD) openssl rand -base64 18 | tr '+/' '-_' ;;
    DASHBOARD_SECRET|N8N_ENCRYPTION_KEY) openssl rand -hex 32 ;;
    *) openssl rand -hex 24 ;;
  esac
}

DASHBOARD_KEYS="DASHBOARD_PASSWORD DASHBOARD_SECRET DASHBOARD_DB_PASSWORD"

add_dashboard() {
  if [ ! -f .env ]; then
    echo "No .env yet: run scripts/init-env.sh first (it includes the dashboard's settings)." >&2
    exit 1
  fi
  changed=""
  header=""
  for key in $DASHBOARD_KEYS COMPOSE_PROFILES; do
    grep -q "^$key=." .env && continue
    # the header's leading newline also ends a last line that has none, so no key gets glued onto it
    if [ -z "$header" ] && ! grep -q "^$key=" .env; then
      printf '\n# Web dashboard, added by scripts/init-env.sh --add-dashboard (see .env.example)\n' >> .env
      header=1
    fi
    if [ "$key" = COMPOSE_PROFILES ]; then put "$key" dashboard .env; else put "$key" "$(new_value "$key")" .env; fi
    changed="$changed $key"
  done
  if ! grep -Eq '^COMPOSE_PROFILES=.*dashboard' .env; then
    echo "COMPOSE_PROFILES already lists other profiles: add dashboard to it yourself (comma-separated)." >&2
  fi
  if [ -z "$changed" ]; then
    echo ".env already has the dashboard's settings; nothing changed."
  else
    echo "Added to .env:$changed (values not shown)."
  fi
  echo "Next: scripts/jobsearch migrate (needs migration 014), docker compose up -d --build, then scripts/tunnel.sh."
  echo "Your login password is the DASHBOARD_PASSWORD line in .env: open the file yourself to read it (keep it out of any chat)."
}

case "${1:-}" in
  "") ;;
  --add-dashboard) add_dashboard; exit 0 ;;
  *) echo "usage: $0 [--add-dashboard]" >&2; exit 2 ;;
esac

if [ -f .env ]; then
  echo ".env already exists, leaving it alone. To add the web dashboard to it: scripts/init-env.sh --add-dashboard" >&2
  exit 0
fi

cp .env.example .env.new
for key in POSTGRES_PASSWORD N8N_ENCRYPTION_KEY $DASHBOARD_KEYS; do
  put "$key" "$(new_value "$key")" .env.new
done
mv .env.new .env

echo "Created .env with generated secrets. Next, put in .env yourself: your time zone (TZ) and ANTHROPIC_API_KEY"
echo "(scripts/set-env.sh ANTHROPIC_API_KEY asks for it with the typing hidden). Then: docker compose up -d --build"
echo "The web dashboard is off; to turn it on, set COMPOSE_PROFILES=dashboard in .env (docs/dashboard.md)."
