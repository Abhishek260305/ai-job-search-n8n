#!/bin/sh
# Check what a commit would publish: no keys, tokens, .env files, personal config or private details.
# Run it before every push (and ask your AI assistant to run it before it commits):
#
#   scripts/check-secrets.sh                          check everything git would add
#   scripts/check-secrets.sh --also "Jane Doe" --also "192.0.2.10"
#                                                     also fail if any of these words appear anywhere
#                                                     (your name, email, server address, tailnet name, ...)
#
# It looks at the files git would add (tracked, plus untracked that are not ignored), so it needs a git repository.
# It prints file names and line numbers only, never the text it matched, so it cannot leak what it finds.
# Exit code 0: nothing found. 1: something found (fix it, then run it again). 2: could not run.
set -u
cd "$(dirname "$0")/.."
command -v git > /dev/null || { echo "git is needed" >&2; exit 2; }
git rev-parse --is-inside-work-tree > /dev/null 2>&1 || { echo "this is not a git repository: run git init first" >&2; exit 2; }

also=""
while [ $# -gt 0 ]; do
  case $1 in
    --also) [ $# -ge 2 ] || { echo "--also needs a word" >&2; exit 2; }; also="$also
$2"; shift 2 ;;
    *) echo "usage: $0 [--also WORD]..." >&2; exit 2 ;;
  esac
done

problems=0
note() { echo "  $1"; problems=$((problems + 1)); }

files=$(git ls-files -co --exclude-standard)
[ -n "$files" ] || { echo "no files to check"; exit 0; }
echo "Checking $(echo "$files" | wc -l | tr -d ' ') files that git would publish..."

# 1. files that must never be published
echo "$files" | while IFS= read -r f; do
  case $f in
    .env.example) ;;
    .env|.env.*|*/.env|*/.env.*) echo "FILE $f: an environment file (it holds your secrets)" ;;
    *.pem|*.key|*.p12|*.pfx|*.ppk|*id_rsa*|*id_ed25519*|*id_ecdsa*|*ssh-key-*|*.kdbx) echo "FILE $f: a key or credential file" ;;
    config/profile.json|config/resume.json|config/search.json|config/search.json.before-pull*|config/.draft/*) echo "FILE $f: your personal config" ;;
    output/*|documents/*) echo "FILE $f: your generated applications or documents" ;;
    backups/*|*.sql.gz|*backup*.sql|*.dump) echo "FILE $f: a database backup (it holds your data)" ;;
    postgres/*.sql) ;;
    *.sql) echo "FILE $f: a .sql file outside postgres/ (a database dump?)" ;;
    *.csv|*.pdf|*.docx|*.zip) echo "FILE $f: a document or export (a resume, a LinkedIn export, a job list?)" ;;
  esac
done > ${TMPDIR:-/tmp}/.check-secrets.$$ 2>/dev/null
if [ -s ${TMPDIR:-/tmp}/.check-secrets.$$ ]; then
  while IFS= read -r line; do note "$line"; done < ${TMPDIR:-/tmp}/.check-secrets.$$
fi
rm -f ${TMPDIR:-/tmp}/.check-secrets.$$

# 2. text that looks like a real key or token (only the shapes real ones have; test dummies are shorter)
scan() {  # scan LABEL REGEX
  echo "$files" | tr '\n' '\0' | xargs -0 grep -nIE -- "$2" 2>/dev/null | cut -d: -f1,2 | while IFS=: read -r file line; do
    echo "LINE $file:$line looks like $1"
  done
}
{
  scan "an Anthropic API key" 'sk-ant-[A-Za-z0-9_-]{30,}'
  scan "an API key" 'sk-[A-Za-z0-9_-]{32,}'
  scan "a Tailscale key" 'tskey-[A-Za-z0-9-]{20,}'
  scan "an Oracle Cloud id" 'ocid1\.[a-z]+\.oc[0-9]\.[A-Za-z0-9._-]{20,}'
  scan "a password in a web address" '[a-z]+://[^/[:space:]:@]+:[^/[:space:]@]{3,}@'
  scan "a bearer token" 'Bearer [A-Za-z0-9._~+/-]{30,}'
  scan "a home folder path" '/(Users|home)/[a-z][a-z0-9._-]{2,}/(Desktop|Documents|Downloads|\.ssh)'
  scan "an AWS access key" 'AKIA[0-9A-Z]{16}'
  scan "a GitHub token" 'gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}'
  scan "a Slack token" 'xox[baprs]-[A-Za-z0-9-]{20,}'
  scan "an Apify token" 'apify_api_[A-Za-z0-9]{20,}'
  scan "a Telegram bot token" '[0-9]{8,10}:[A-Za-z0-9_-]{35}'
  scan "a private key" '-----BEGIN [A-Z ]*PRIVATE KEY-----'
  scan "a JSON web token" 'eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}'
  scan "a Google API key" 'AIza[0-9A-Za-z_-]{35}'
} > ${TMPDIR:-/tmp}/.check-secrets.$$
if [ -s ${TMPDIR:-/tmp}/.check-secrets.$$ ]; then
  while IFS= read -r line; do note "$line"; done < ${TMPDIR:-/tmp}/.check-secrets.$$
fi
rm -f ${TMPDIR:-/tmp}/.check-secrets.$$

# 2b. binary files are not searched above (a screenshot or a PDF resume could hold anything): list them
is_binary() {  # a non-empty file with no text in it (images and fonts that ship with the dashboard are fine)
  case $1 in
    *.png|*.jpg|*.jpeg|*.gif|*.ico|*.woff|*.woff2|*.ttf|*.svg|dashboard/web/public/*) return 1 ;;
  esac
  [ -s "$1" ] && ! grep -qI . "$1" 2> /dev/null
}
bin=$(echo "$files" | while IFS= read -r f; do
  if [ -f "$f" ] && is_binary "$f"; then echo "$f"; fi
done)
for f in $bin; do note "FILE $f: a binary file, which this check cannot read: open it yourself before you publish"; done

# 3. .env.example must hold no real value for a secret
if echo "$files" | grep -qx '.env.example'; then
  bad=$(grep -nE '^(ANTHROPIC_API_KEY|TELEGRAM_BOT_TOKEN|APIFY_TOKEN_[A-Z]+|DASHBOARD_PASSWORD|DASHBOARD_SECRET|DASHBOARD_DB_PASSWORD)=.+' .env.example | cut -d: -f1)
  for n in $bad; do note ".env.example:$n gives a secret a value (it must stay empty)"; done
fi

# 4. words you named with --also (your name, email, server address, ...)
echo "$also" | while IFS= read -r word; do
  [ -n "$word" ] || continue
  echo "$files" | tr '\n' '\0' | xargs -0 grep -nIiF -- "$word" 2>/dev/null | cut -d: -f1,2 | while IFS=: read -r file line; do
    echo "LINE $file:$line has one of the words you gave with --also"
  done
done > ${TMPDIR:-/tmp}/.check-secrets.$$
if [ -s ${TMPDIR:-/tmp}/.check-secrets.$$ ]; then
  while IFS= read -r line; do note "$line"; done < ${TMPDIR:-/tmp}/.check-secrets.$$
fi
rm -f ${TMPDIR:-/tmp}/.check-secrets.$$

if [ "$problems" -gt 0 ]; then
  echo
  echo "$problems problem(s). Do not push yet: remove or ignore these, then run this again."
  echo "If a key was ever committed, rotate it (the old one stays in git history)."
  exit 1
fi
echo "Nothing found. (This is a safety net, not a guarantee: read 'git status' before you push.)"
exit 0
