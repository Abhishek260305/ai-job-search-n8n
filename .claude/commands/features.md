---
description: Show or change which optional parts of the pipeline are on (tailoring, Telegram, the daily digest).
argument-hint: [tailoring|telegram|digest on|off ...]
---

# /features

Collecting, filtering and scoring are the core and always run. Everything after them is optional.

1. Run `scripts/jobsearch features` and explain, in plain words, which parts are on and why any part is off (switched off by the user, or not configured, such as no Telegram token in `.env`).
2. If `$ARGUMENTS` asks for a change (for example `tailoring off`), run `scripts/jobsearch features set --tailoring off` (the options are `--tailoring`, `--telegram`, `--digest`, each `on` or `off`). Changes apply immediately and are stored in the database.
3. Explain what the change means:
   - **tailoring off**: the pipeline stops at scoring; shortlisted jobs wait in the database (`scripts/jobsearch jobs`). Nothing is generated and no resume or cover-letter model calls are made.
   - **telegram off, or not configured**: no messages, no digest, no commands. Review at the command line (`review`, `approve`, `skip`, `applied`, `mark`). Approval `review` then behaves as `auto` (stored ready); `cli` keeps a human decision.
   - **digest off**: no morning message; Telegram commands still work.
4. If the user wants Telegram but it is "not configured", point them to `docs/setup.md` step 3 and have them add the token and chat id to `.env` themselves, then run `docker compose up -d scraper` and `scripts/n8n-import-workflows.sh --publish`. Never ask for the token in chat.

Switching a part on never spends money by itself, but tailoring and Telegram review do act on the next scheduled run: say so.
