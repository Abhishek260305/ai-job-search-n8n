#!/bin/sh
# Import the six workflows into n8n, filling in your Telegram chat id from .env.
#
#   scripts/n8n-import-workflows.sh [--publish]
#
# Reads TELEGRAM_CHAT_ID from .env (override the file with ENV_FILE=...) and writes it into the
# digest workflow's "Send to Telegram" node in a temporary copy, so the files in workflows/ stay free
# of personal data. Re-running updates the workflows in place (they have fixed ids).
#
#   --publish   also publish (schedule) the workflows and restart n8n, which is what makes the
#               schedules take effect.
#
# 00 is the scheduler: it starts 01 collect, 02 score, 03 digest and 04 tailor on the schedule stored in the
# database (scripts/jobsearch schedule), so changing a schedule never needs this script or an n8n restart.
#
# Telegram is optional. The two Telegram workflows (03 digest, 05 commands) are published only when both
# TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set, and are unpublished otherwise (the scheduler never starts
# the digest without Telegram). The scheduler, collect (01), score (02) and tailor (04) are always published;
# tailoring does nothing while it is switched off (scripts/jobsearch features set tailoring off).
set -e
cd "$(dirname "$0")/.."
ENV_FILE="${ENV_FILE:-.env}"
COMPOSE="${JOBSEARCH_COMPOSE:-docker compose}"
PUBLISH=0
[ "$1" = "--publish" ] && PUBLISH=1

val() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -n 1 | cut -d= -f2- | tr -d '\r' | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'\$//"; }
CHAT="$(val TELEGRAM_CHAT_ID)"
TOKEN="$(val TELEGRAM_BOT_TOKEN)"
TELEGRAM=0
[ -n "$CHAT" ] && [ -n "$TOKEN" ] && TELEGRAM=1

[ -n "$CHAT" ] && [ -z "$TOKEN" ] && echo "note: TELEGRAM_CHAT_ID is set but TELEGRAM_BOT_TOKEN is empty in $ENV_FILE, so Telegram stays off (it is optional)." >&2
case "$CHAT" in
  "") echo "note: Telegram is not set up (no TELEGRAM_CHAT_ID in $ENV_FILE). That is fine: it is optional. The digest and Telegram-command workflows will not be published; use the CLI instead (scripts/jobsearch jobs, review, approve)." >&2 ;;
  *[!0-9-]*) echo "TELEGRAM_CHAT_ID must be a number (it may start with '-' for groups)" >&2; exit 1 ;;
esac

# stage copies of the workflow files inside the container, with the chat id filled in
$COMPOSE exec -T n8n sh -c 'rm -rf /tmp/wf && mkdir -p /tmp/wf && cp /workflows/*.json /tmp/wf/'
if [ -n "$CHAT" ]; then
  $COMPOSE exec -T n8n sh -c "sed -i 's/REPLACE_WITH_YOUR_CHAT_ID/$CHAT/' /tmp/wf/03-daily-digest.json"
fi
$COMPOSE exec -T n8n sh -c 'n8n import:workflow --separate --input=/tmp/wf/ 2>&1 | grep -E "Successfully|rror" || true; rm -rf /tmp/wf'

if [ "$PUBLISH" = 1 ]; then
  for id in js-scheduler js-collect js-score js-tailor; do
    $COMPOSE exec -T n8n sh -c "n8n publish:workflow --id=$id 2>&1 | grep -E 'Publishing|rror' || true"
  done
  for id in js-telegram js-digest; do
    if [ "$TELEGRAM" = 1 ]; then
      $COMPOSE exec -T n8n sh -c "n8n publish:workflow --id=$id 2>&1 | grep -E 'Publishing|rror' || true"
    else
      $COMPOSE exec -T n8n sh -c "n8n unpublish:workflow --id=$id 2>&1 | grep -E 'npublish|rror'" || true
    fi
  done
  $COMPOSE restart n8n
  if [ "$TELEGRAM" = 1 ]; then
    echo "published and restarted n8n: the scheduler, collect, score, tailor, digest and Telegram workflows are live."
  else
    echo "published and restarted n8n: the scheduler, collect, score and tailor are live (the Telegram workflows are off)."
  fi
fi
