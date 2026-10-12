#!/bin/sh
# Type a secret into .env without it showing on screen, in your shell history, or in an AI chat.
#
#   scripts/set-env.sh ANTHROPIC_API_KEY       asks for the value (nothing is echoed), then writes it into .env
#   scripts/set-env.sh TELEGRAM_BOT_TOKEN
#
# Run it yourself in a terminal (in Claude Code: type  ! scripts/set-env.sh ANTHROPIC_API_KEY  so the prompt reaches you).
# An assistant should never ask you to paste a key into the chat: it points you at this script, or at .env itself.
# Only the key name is printed, never the value. The key must already be a line in .env (see .env.example).
set -e
cd "$(dirname "$0")/.."
umask 077
trap 'rm -f .env.tmp; [ -z "${saved_tty:-}" ] || stty "$saved_tty" 2> /dev/null' EXIT
case "${1:-}" in
  ''|*[!A-Z0-9_]*) echo "usage: $0 KEY   (for example ANTHROPIC_API_KEY)" >&2; exit 2 ;;
esac
KEY=$1
[ -f .env ] || { echo "No .env yet: run scripts/init-env.sh first." >&2; exit 1; }
grep -q "^$KEY=" .env || { echo "$KEY is not a line in .env. Check the name against .env.example." >&2; exit 1; }

if [ -t 0 ]; then
  saved_tty=$(stty -g)
  stty -echo
  printf 'Value for %s (typing is hidden): ' "$KEY" >&2
  IFS= read -r value || true
  stty "$saved_tty"; saved_tty=""
  echo >&2
else
  IFS= read -r value || true   # piped in (used by the tests)
fi
[ -n "$value" ] || { echo "Nothing entered; .env is unchanged." >&2; exit 1; }
case $value in
  *[[:space:]]*|*\'*|*\"*|*\\*|*\$*|*\`*) echo "That value has a space or a quote or another character that breaks .env. Real keys have none: check it and try again." >&2; exit 1 ;;
esac

# the value goes through the environment, not the command line, so it never shows up in ps
K="$KEY" V="$value" awk '!done && index($0, ENVIRON["K"] "=") == 1 { print ENVIRON["K"] "=" ENVIRON["V"]; done = 1; next } { print }' .env > .env.tmp
mv .env.tmp .env
echo "$KEY saved in .env (value not shown)."
