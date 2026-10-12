#!/bin/sh
# Back up everything that is not in git to backups/ (compressed, readable by you only, the newest 14 of each kept):
#   jobsearch-<time>.sql.gz   the jobs database (jobs, scores, applications, settings)
#   n8n-<time>.sql.gz         n8n's own database (your workflows, owner account, saved credentials)
#   config-<time>.tar.gz      config/profile.json, resume.json and search.json (only the ones that exist)
#
#   scripts/backup.sh                    one backup now
#   scripts/backup.sh --keep 30          keep the newest 30
#   scripts/backup.sh --dir /mnt/usb     write somewhere else
#
# Run it every night: crontab -e, then add (use your own folder):
#   15 3 * * * cd /home/you/ai-job-search-n8n && scripts/backup.sh >> backups/backup.log 2>&1
# Restore: docs/setup.md#backups. Copy backups/ off this machine, and keep a copy of .env somewhere safe
# (N8N_ENCRYPTION_KEY!): the backup holds the n8n credentials only in encrypted form and .env is not in it.
set -eu
cd "$(dirname "$0")/.."
umask 077
KEEP=14
DIR=backups
while [ $# -gt 0 ]; do
  case $1 in
    --keep) [ $# -ge 2 ] || { echo "--keep needs a number" >&2; exit 2; }; KEEP=$2; shift 2 ;;
    --dir) [ $# -ge 2 ] || { echo "--dir needs a folder" >&2; exit 2; }; DIR=$2; shift 2 ;;
    *) echo "usage: $0 [--keep N] [--dir DIR]" >&2; exit 2 ;;
  esac
done
case $KEEP in ''|*[!0-9]*|0) echo "--keep must be a whole number of at least 1" >&2; exit 2 ;; esac

COMPOSE=${JOBSEARCH_COMPOSE:-docker compose}
mkdir -p "$DIR"
stamp=$(date +%Y%m%d-%H%M%S)
tmp=""
trap 'rm -f "$DIR"/*.tmp' EXIT

# dump_db DATABASE PREFIX: the password never appears here, the container reads it from its own environment
dump_db() {
  out="$DIR/$2-$stamp.sql.gz"
  tmp="$out.tmp"
  $COMPOSE exec -T postgres sh -c "pg_dump -U \"\$POSTGRES_USER\" $1" | gzip > "$tmp" || true
  size=$(wc -c < "$tmp" | tr -d ' ')
  # a failed dump leaves an empty archive (about 20 bytes); even an empty schema compresses to far more
  if [ "$size" -lt 200 ] || ! gzip -t "$tmp" 2> /dev/null; then
    echo "backup failed: the $1 dump is empty or damaged (is the stack running? docker compose ps)" >&2
    exit 1
  fi
  mv "$tmp" "$out"
  echo "backup written: $out ($size bytes)"
}

dump_db jobsearch jobsearch
dump_db n8n n8n

# your hand-made config (gitignored, so it exists nowhere else)
present=""
for f in profile.json resume.json search.json; do [ -f "config/$f" ] && present="$present config/$f"; done
if [ -n "$present" ]; then
  out="$DIR/config-$stamp.tar.gz"
  # COPYFILE_DISABLE: macOS tar would add ._ metadata files
  COPYFILE_DISABLE=1 tar -czf "$out.tmp" $present && mv "$out.tmp" "$out"
  echo "backup written: $out"
else
  echo "no config/*.json found, so no config backup (nothing to lose)"
fi

# keep the newest $KEEP of each kind
for prefix in jobsearch n8n config; do
  ls -1t "$DIR"/$prefix-*.gz 2> /dev/null | tail -n +"$((KEEP + 1))" | while IFS= read -r old; do rm -f "$old"; done
done
echo "the newest $KEEP of each are kept in $DIR"
