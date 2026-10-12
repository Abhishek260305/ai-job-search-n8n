#!/bin/sh
# Create the n8n credentials the workflows need from values in .env, so nothing is typed into a UI
# or pasted into chat. Run it on the machine that hosts the docker compose stack:
#
#   scripts/n8n-credentials.sh
#
# Reads from .env (override the file with ENV_FILE=...):
#   ANTHROPIC_API_KEY     -> n8n credential "Anthropic API key" (Header Auth: x-api-key)
#   TELEGRAM_BOT_TOKEN    -> n8n credential "Telegram bot"       (Telegram API)   [optional]
# Safe to re-run: it updates the credentials in place. n8n encrypts them with N8N_ENCRYPTION_KEY.
# Prefer the UI? See "Adding the credentials in the n8n UI instead" in docs/workflows.md.
set -e
cd "$(dirname "$0")/.."
ENV_FILE="${ENV_FILE:-.env}"
COMPOSE="${JOBSEARCH_COMPOSE:-docker compose}"

[ -f "$ENV_FILE" ] || { echo "no $ENV_FILE found" >&2; exit 1; }
# read a value without sourcing the file (it is data, not code)
val() { grep -E "^$1=" "$ENV_FILE" | tail -n 1 | cut -d= -f2- | tr -d '\r' | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'\$//"; }

ANTHROPIC="$(val ANTHROPIC_API_KEY)"
TELEGRAM="$(val TELEGRAM_BOT_TOKEN)"

# Allow only the characters real keys use, so a value can never alter the JSON we build.
case "$ANTHROPIC" in
  "") echo "ANTHROPIC_API_KEY is empty in $ENV_FILE: add it first (nano $ENV_FILE)" >&2; exit 1 ;;
  *[!A-Za-z0-9_-]*) echo "ANTHROPIC_API_KEY contains unexpected characters; paste only the key" >&2; exit 1 ;;
esac
case "$TELEGRAM" in
  "") ;;
  *[!0-9A-Za-z:_-]*) echo "TELEGRAM_BOT_TOKEN contains unexpected characters; paste only the token" >&2; exit 1 ;;
esac

JSON='[{"id":"js-anthropic-key","name":"Anthropic API key","type":"httpHeaderAuth","data":{"name":"x-api-key","value":"'"$ANTHROPIC"'"}}'
if [ -n "$TELEGRAM" ]; then
  JSON="$JSON"',{"id":"js-telegram-bot","name":"Telegram bot","type":"telegramApi","data":{"accessToken":"'"$TELEGRAM"'","baseUrl":"https://api.telegram.org"}}'
fi
JSON="$JSON]"

# the file only ever lives inside the container, and is removed afterwards
out=$(printf '%s' "$JSON" | $COMPOSE exec -T n8n sh -c \
  'umask 077; cat > /tmp/jobsearch-creds.json; n8n import:credentials --input=/tmp/jobsearch-creds.json 2>&1; rm -f /tmp/jobsearch-creds.json') || true
echo "$out" | grep -v -E "Postgres 16|migration lock" || true
echo "$out" | grep -q "Successfully" || {
  echo "n8n did not confirm the import (is the stack up? docker compose ps). Nothing was created." >&2
  exit 1
}

echo "Credentials created: Anthropic API key$( [ -n "$TELEGRAM" ] && echo ', Telegram bot')."
[ -n "$TELEGRAM" ] || echo "(No TELEGRAM_BOT_TOKEN set: add it later and re-run if you want the daily digest.)"
