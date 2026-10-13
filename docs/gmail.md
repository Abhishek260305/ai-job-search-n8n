# Gmail status tracking (optional, Phase 6)

**Status: the foundations (6a), the Google login (6b), the search, matcher and rules (6c), the model step (6d) and the scheduled stage (6e) are built and deployed. The dashboard's Email page (6f) is deployed, so suggestions can be accepted or dismissed there. 6g, the first real run on your mailbox, is next.** Switching the part on today checks that the login works and reads no email. This page says what exists, what is planned, and the rules the rest will be built to, so you can judge it before it exists.

The goal: after you apply, replies arrive by email (an interview invitation, an assessment, a rejection). Instead of you moving each job to the right stage by hand, the system reads those replies and **suggests** the change. You accept or dismiss each suggestion. Nothing changes without you.

It is optional like every other part after scoring. Without it you keep the manual path (`scripts/jobsearch mark`, or the dashboard's board) and lose nothing.

## What exists now (step 6a)

| Piece | What it is |
|---|---|
| **A switch, off by default** | `scripts/jobsearch features set --gmail on`. Unlike the other parts it starts **off**: a part that needs a Google login must be asked for. It is also off while the login is not in `.env`, and `scripts/jobsearch features` says which. |
| **A `mail` stage in the scheduler** | One more stage next to collect, score, tailor and digest: every 30 minutes by default, at most 20 emails opened per run, never more often than every 15 minutes (`scripts/jobsearch schedule set mail --every 1h`). The scheduler does not start it while the part is off, so it starts promptly once the part is on. Workflow `06 Check mail` is what it starts. |
| **`scripts/jobsearch gmail`** | The state of the part: switch, login, where the search stands, how many suggestions wait. `gmail lookback 30` or `gmail lookback from-now` sets how the **first** search starts (below). `gmail run` asks for a run at the next scheduler tick, and says why when that is not possible. |
| **Three tables** (migration 021) | `gmail_state` (one row: the settings and the health of the login), `gmail_seen` (every email already looked at, by Gmail's message id, so none is handled twice) and `email_suggestions` (what the reader proposes). The dashboard's database role cannot read any of them. |
| **Doctor line** | When the switch is on, `doctor` reports the part: not configured, login expired, last check failed, or how many suggestions wait. When it is off, the doctor's "optional parts" line lists it as off and nothing more. |

What it does **not** do yet: read an email, call Claude, or write a suggestion. The scheduled stage runs, checks the saved login, records the result and says so (`the Gmail reader is not built yet`).

## How it will work (the plan, and the rules it is built to)

1. **A read-only Google permission.** The login is an OAuth client plus a refresh token, kept in `.env` (never in git). The permission asked for is `gmail.readonly`: the system cannot send, delete, label, archive or mark anything as read.
2. **A search, not a download.** Each run asks Gmail for specific mail, never the whole inbox: mail that mentions the companies of your open applications, or that comes from the usual job-site senders (Greenhouse, Lever, Workday, Ashby, SmartRecruiters and the like), excluding what you sent. Archived mail counts; spam and trash do not.
3. **The first search looks back, then moves forward.** Replies to applications you made before connecting matter most, so the first search looks back `gmail lookback` days (60 by default, 1 to 365). `gmail lookback from-now` instead starts the clock and fetches nothing: only mail that arrives afterwards is looked at. Later searches continue from where the last one began, and a table of handled message ids makes the overlap harmless.
4. **Only what is needed is kept.** For each matched email: the sender's **domain**, the subject, the date and an excerpt of a few hundred characters. Never the whole message.
5. **A match must be confident.** An email is matched to an application by company name and sender, with whole-word matching so a short name such as "HP" never matches "PHP". Anything ambiguous is recorded as *unmatched* and shown to nobody as a suggestion.
6. **Rules first, Claude second.** Plain rules recognise the obvious cases for free. Claude (Haiku) sees only the unclear ones, returns a strict JSON answer that code validates, and **cannot choose the status**: code maps the signal to a proposed status. The email text is untrusted data, fenced like a job posting, so an email cannot give the classifier instructions.
7. **What can be suggested.** An acknowledgement (moves a job still marked *drafted* to *applied*), an assessment or interview (to *interview*), an offer (to *offer*), a rejection (to *rejected*). **Nothing after an offer: whether you accept or decline is your decision, not something an email can prove.** The database itself only allows these four, and fixes which status each kind of email maps to (an acknowledgement can only mean *applied*, a rejection only *rejected*). A signal that contradicts a final status (a rejection after an offer, say) is flagged for you instead of being proposed.
8. **You decide.** Suggestions appear in the dashboard (and as a line in the Telegram digest). *Accept* moves the job through the same function as a click on the board, so it is recorded in the job's history and the Undo button works. *Dismiss* closes it. A suggestion whose job changed in the meantime is refused as stale.
9. **Offers get a phishing check.** An email that looks like an offer is flagged when the sender does not match the company, when the sender's domain fails authentication, or when it asks for money.

## Planned steps

| Step | What | State |
|---|---|---|
| **6a** | The foundations above | ✅ deployed (2026-10-12) |
| **6b** | The Gmail client and the one-time sign-in, tested against a stand-in Google (expired login, over-broad permission, redirects, rate limits, garbage and huge replies) | ✅ deployed (2026-10-12; not tried against real Google yet) ([below](#the-google-login-6b)) |
| **6c** | The search, the matcher and the rules classifier, with attack tests (look-alike sender, "offer" asking for money, prompt injection) | ✅ deployed (2026-10-12; migration 022) ([below](#the-search-and-the-rules-6c)) |
| **6d** | Claude classification under the spending caps: counted attempts, parked messages, per-run and per-day limits | ✅ deployed (2026-10-13, migration 023) ([below](#the-model-step-6d)) |
| **6e** | Wiring: the scheduled stage reads mail (search, then the model step), the doctor, the Telegram digest line | ✅ deployed (2026-10-13, migration 024) ([below](#the-scheduled-stage-6e)); suggestions are accepted on the dashboard's Email page (6f) |
| **6f** | The dashboard's email inbox: accept, dismiss, stale, undo; the switch and the `mail` schedule appear on the Settings page | ✅ deployed (2026-10-13, migration 025) ([dashboard.md](dashboard.md#version-6f-the-email-inbox)) |
| **6g** | Docs, review, deploy, and one real run on your mailbox | ⬜ |

## The Google login (6b)

**`scripts/jobsearch gmail login`** signs in once, on your own computer (the one with a browser), and saves the login in `.env` on the machine that runs the stack (over SSH when `JOBSEARCH_SSH` is set, like the other commands).
- It asks for the client id and secret of the Google OAuth client you create below (the secret is typed hidden, or read from `GMAIL_CLIENT_ID` and `GMAIL_CLIENT_SECRET` in your shell).
- It opens Google's page, which asks you to approve one permission: *View your email messages and settings*. The answer comes back to a small web server it runs on `127.0.0.1` only, for a few minutes, using a random state value and PKCE, so an answer meant for anything else is ignored.
- It **refuses** a login that is more than read-only, or one without a refresh token, and shows the account as `j***@gmail.com` so you can stop if it is the wrong one. If you answer no there, nothing is saved here, but Google still lists the sign-in as approved: remove the app at <https://myaccount.google.com/permissions> if you do not want it.
- It saves the three values by feeding each to `scripts/set-env.sh` on standard input. They are never printed, never an argument of a command, never written to any file other than that `.env` (which is on your computer only when the stack runs there), and never sent anywhere but Google and that `.env`. `--no-browser` prints the link only; `--restart` also restarts the scraper so it sees the new values (otherwise it tells you the command).

**`scripts/jobsearch gmail check`** tests the saved login with one profile request (no email is read), records the result for the doctor, and says which account it is. It works before you switch the part on. A login Google no longer accepts (expired, revoked) is reported as such, with the command to sign in again.

### Creating the Google OAuth client (what only you can do)

These are the steps in Google's console as I understand them; Google renames its screens now and then, so follow the meaning if a label differs, and tell me if something does not match.
1. In the [Google Cloud console](https://console.cloud.google.com/) create a project (any name). No billing or card is needed for the Gmail API.
2. **APIs & Services → Library**: find *Gmail API* and **Enable** it.
3. **OAuth consent screen** (now under *Google Auth Platform*): choose **External**, give it a name and your own address as support and developer contact. Under scopes add only `https://www.googleapis.com/auth/gmail.readonly` (Google classes it as a *restricted* scope). Add your own Gmail address as a **test user**.
4. **Publish the app** (*In production*). While it stays in *Testing*, Google ends the login after **7 days**, which would break unattended use. For a personal app that only you use, Google shows an "unverified app" warning when you approve (*Advanced → Go to … (unsafe)*), because the permission is a restricted one and the app has not been through Google's review; the warning is about the app being unreviewed, and you are the author. An unverified app is also limited to about 100 users, which does not matter here. I believe this removes the 7-day limit but have not been able to confirm it against your account: if the login still stops after a week, that is the cause.
5. **Credentials → Create credentials → OAuth client ID**, application type **Desktop app**. Copy the client id and the client secret (do not paste them into any chat; `gmail login` asks for them).
6. Run `scripts/jobsearch gmail login`, approve in the browser, confirm the account, then `scripts/jobsearch gmail check`, and `scripts/jobsearch features set --gmail on` when you want the part on.

If Google later stops accepting the login (a password change, or you removed the app at <https://myaccount.google.com/permissions>), the doctor says so and `gmail login` signs in again.

### What the code guarantees (all tested against a stand-in Google that fails on purpose)

- Only two Google hosts are ever called, compared as parsed origins (the test override `GMAIL_API_URL` is honoured only for a loopback address, because the login is sent wherever it points); a redirect is never followed; every response is size-limited and time-limited; a 429 is never retried and a server error is retried once.
- The client has no call that sends, deletes, labels or marks anything; a login whose answer grants more than read-only is refused before it is used.
- No token, secret, search text or email text is logged; error messages are scrubbed of anything shaped like a Google credential before they are stored or shown.
- An email is untrusted data: text inside HTML that the markup itself hides is dropped and markup that cannot be followed like a browser is marked `markup_doubt` (see [the model step](#the-model-step-6d)), so the reader sees what a person sees; its text is capped; control characters and characters that render as nothing (zero-width and bidirectional marks, the soft hyphen, variation selectors, the Unicode "tag" block) are removed; a charset a sender names is honoured only if it is a real mail charset (anything else is read as UTF-8, so it cannot crash the reader or hide text); and a sender domain written in non-ASCII letters counts as no domain, so a look-alike can never equal a real one.
- The only authenticity signal taken from an email is the verdict Google's own server recorded. Google adds exactly one `Authentication-Results` header; a message with two that claim to be Google's is treated as untrusted, and the verdicts are read per segment with comments and quoted strings removed, so text a sender can influence inside the header cannot supply one. `Message.sender_verified` is true only when DMARC passed for exactly the domain that was read as the sender. This is evidence that the domain is not forged, not proof that the email is wanted.
- `scripts/check-secrets.sh` now also catches Google client secrets, refresh and access tokens and client ids before a push.

## The search and the rules (6c)

Built and tested; run by the scheduled stage (step 6e): `scraper/mail_search.py` (the search loop), `mail_match.py` (which application an email is about) and `mail_rules.py` (what it says, what it may change, whether to trust it). Migration 022 adds `gmail_candidates()`, `gmail_record_result()`, `gmail_rescan()`, the `unclear` outcome and a unique index. No Claude call is made anywhere in it.

**Which applications can get a reply.** Submitted ones (applied, in progress, interview, offer), the final ones (rejected, withdrawn: a late email about one is shown as a conflict) and jobs that were shortlisted, tailored or approved but never marked applied (you may have applied by hand; an acknowledgement then proposes "applied"). Never a skipped or scam-blocked job. The 100 most active are searched for.

**The search.** Each run builds up to eight Gmail searches: one for the usual applicant-tracking senders (Greenhouse, Lever, Workday, Ashby, SmartRecruiters and the like) and up to seven for your company names. A company is searched for as *the sender or in the subject* (`from:acme subject:acme`), never as a bare word, so a company called "Amazon" does not pull in every order email that mentions it. Placeholders, very short names and generic one-word names such as "Global" are not searched by name, and names that do not fit are counted in the run's summary, never hidden (while any are left out the cursor does not move). Every search excludes what you sent. The first run looks back `gmail lookback` days; later runs start a day before the last one began (in "only mail from now on" mode, ten minutes, so mail from before you switched it on is never read). Results are read newest first, the ones already looked at are skipped, and a run goes only as deep as it needs to in order to find `batch` new emails (default 20 opened per run). A search that fills up carries on from the date of its last result, so a mailbox of any size is reached, oldest mail included, with each result read once; Gmail calls per run are capped at 120. A big first run is finished over several runs (a 605-email mailbox takes 31 runs at the default, about 15 hours; 1,300 matches take 14 runs of 100). The cursor only moves when a run covered everything, and not at all while there is no application that could get a reply.

**Old mail.** The search can only find mail it is looking for at the time. If an application became a candidate *after* the cursor moved past a reply (you applied by hand to a job you never shortlisted, say), run `scripts/jobsearch gmail rescan`: it searches the first-search window again and re-opens the emails that were read but not acted on (noise, unmatched, unclear). Suggestions already made are untouched.

**Which application.** An email must carry evidence worth 3 points: the sender's domain *is* the company (3), the company is the sender's display name (2), is in the subject (2) or in the first 700 characters of the text (1), plus 1 when two or more words of the job title appear together. The domain counts when its owner label is the company's whole name (`acme.com`, `acme-corp.com`, `acmecorp.com` for "Acme Corp"; not `a-c-m-e.com`); only the first distinctive word of a longer name (`delta.com` for "Delta Dental") is worth 2 and is flagged. Names match as whole words after normalisation (so "HP" never matches inside "PHP", and a name of three letters or fewer must appear as written); a name of eight letters or more also matches when its words are written run together or spaced apart ("JPMorganChase" stored, "JPMorgan Chase & Co." in the mail, or the reverse). A sender on an applicant-tracking system's domain (Greenhouse, Lever, Workday, Oracle's recruiting hosts `*.cloud.oracle.com`, and the like) is never *the company's* domain, whatever the company is called: the company has to be named in the mail, placeholder companies (`?`, "Confidential", "Stealth") match nothing, and a generic name counts only through the domain. Two companies that fit about equally, or several applications at one company that the email does not tell apart, are recorded as *unmatched*, never guessed. Confidence is *high* only when the sender's domain is the company's whole name with a familiar ending, or the company's applicant-tracking system with several pieces of evidence; anything else is at most *medium*, and an unfamiliar ending (`acme.xyz`) is flagged.

**What this cannot know.** Nothing here can tell who *registered* a domain: a look-alike such as `acme.xyz`, or a domain that happens to match a different company with a similar name, passes the name test (it is flagged, never high). Mail that an attacker sends *through* an applicant-tracking account (Greenhouse, Lever and the like authenticate as themselves) with the company's name in the display name and subject scores as a genuine ATS mail. A name that is also the start of a longer one ("Acme" for a mail from "Acme Robotics") can match the wrong company when only the shorter one is yours. Google's own check that a sender is who it claims, and the flags, are the safeguards; you accept or dismiss every suggestion.

**What it says.** Wording lists name one of: acknowledgement, assessment, interview, offer, rejection, or noise (a job alert or newsletter). A bare "offer" is never an offer ("unable to offer you" is a rejection; "pleased to offer you" counts only when it names the job); "unfortunately" alone is not a rejection; a "thank you for applying" template that then says no (in any of the common ways) is a rejection, flagged. Wording that contradicts itself, or only hints, is recorded as *unclear* for step 6d. Footers such as "view all jobs" or "subscribe to our newsletter" never hide a real signal. Only the top of the email is read.

**What a signal may change.** Fixed in code, not by the classifier:

| Signal | Proposes | From | Otherwise |
|---|---|---|---|
| acknowledgement | applied | no application, drafted, approved | nothing (already submitted) |
| interview / assessment | interview | no application, drafted, approved, applied, in progress | nothing at interview or offer; a **conflict** at rejected or withdrawn |
| offer | offer | anything not final | nothing at offer; a **conflict** at rejected or withdrawn |
| rejection | rejected | anything except below | nothing at rejected or withdrawn; a **conflict** at offer (never overwritten) |

A conflict is stored as a suggestion flagged `conflict`, to be shown for review, not for one-click acceptance.

**Flags** (at most ten per suggestion): `unverified_sender` (Google did not verify the sender's domain; the confidence is then never high), `sender_domain_not_the_company`, `free_mail_sender`, `display_name_only`, `unfamiliar_domain_ending`, `domain_matches_first_word_only`, `role_mismatch` (the email names a role, in its subject or else in the first lines of its text, that the matched job's title does not cover: "Software Engineer, Rulesets" for a job titled "Software Engineer". An email may shorten a title but not extend it, and one that names no role is not flagged. The confidence is then *low*, so the Email page asks you to acknowledge it before accepting; a reply about a role the pipeline never collected otherwise lands on a same-company job), `scam_pattern` (the scam wording rules of [safety.md](safety.md): fees, ID or bank details, chat-app contact), `suspicious_link` (checked over every link in the email, including the targets of HTML buttons and links in an HTML part that the plain text does not show: a shortener, a look-alike of a job site, a chat app, a raw IP, or a host that only *contains* the company's name), `injection_attempt`, `markup_doubt` (the HTML could not be read the way a person sees it: the confidence is then low) (text aimed at an AI), `template_mixed_signals`, `date_from_email` (an acknowledgement's date is only an upper bound on when you applied), `conflict`, and `phishing_risk`, set for an offer with any doubt about its sender and for any email with a scam pattern or suspicious link; it makes the confidence *low*.

**What is kept.** Per email: the sender's domain, the subject, the date, a short excerpt around the deciding wording and the flags. The excerpt has links (with or without a scheme), email addresses, phone numbers and anything that looks like a code (a word of four or more characters containing a digit: one-time codes, tokens, ids; years are kept) removed from the whole text before it is cut, so no fragment survives. There is at most one pending suggestion per job and status. If a later email for the same thing is *more* trustworthy than the pending one (higher confidence), it takes its place, so a verified email is not lost behind an earlier spoof; an equal or lower one only records that it was looked at. Three "thanks for applying" emails make one suggestion.

**One bad email cannot stall the pipeline.** An email the decision code cannot handle, or a suggestion the database refuses, is recorded as *failed* and the run goes on; it is never met again on every run.

**Not verified against real Gmail yet.** The `from:` / `subject:` search terms, including the parenthesised group used for a name of several words (`from:(acme robotics)`), follow Gmail's documented search syntax but have only been run against the stand-in. The first real run (step 6g) shows whether they find what they should. The matcher gives a body-only mention one point of three, so mail found only by one could never match anyway.

**Proved by tests.** Against the stand-in Gmail: every outcome, idempotence, the cursor and the overlap, the bounds, a hostile mailbox, a mailbox with more matches than one search can page, fake rejections from unrelated senders, a look-alike "offer" that asks for a fee and links a shortener (low confidence, called phishing), free mail posing as the company, instructions aimed at an AI (nothing is proposed because of them), hidden text, and a bad email in the middle of a run. The runaway rehearsal runs the real SQL: a 605-email mailbox is opened exactly once per email and at most 20 per run, 1,300 matches in one window are finished with the oldest reply included, replays create no duplicate, expired-login and rate-limit runs write nothing, and no Claude call is logged.

## The model step (6d)

Built and tested; run by the scheduled stage (step 6e): `scraper/mail_ai.py` and migration 023 (`gmail_claim_unclear`, `gmail_release_classify_claim`, `gmail_settle_unclear`, `gmail_unclear_counts`, `gmail_classified_today`; none of them runnable by the dashboard's database role).

**What it is for.** The rules of 6c leave some matched emails *unclear*: they are about one of your applications, but the wording does not decide what they say ("we have completed our review", a reply in another style). Those rows wait in `gmail_seen` as `unclear`. The model step asks Claude Haiku about each, one email per call, and turns the answer into the same kind of suggestion the rules make, or into nothing.

**What the model can and cannot do.**
- It answers with one line of JSON: a **signal** (acknowledgement, assessment, interview, offer, rejection or none), how sure it is, and one short **quote**. It never names a status and never writes a word that is stored.
- Code maps the signal to a status with the same table as the rules (above). The stored excerpt is cut from the email by code; the quote is only a witness, and must appear **word for word in the email** (case, spacing and curly quotes aside) or the answer is refused.
- Anything else is an *unusable answer*: another signal name, a confidence that is not high/medium/low, a missing or invented quote, a reply cut off by the token limit. It is logged as a failed call, the email stays unclear, and it is asked about again at most twice more (below).
- **A model suggestion is never high confidence.** It is at most medium, is marked `classified_by_model` and `source = model` (the inbox will say so), and goes through the same checks as a rules suggestion: sender verification, look-alike links, scam wording, the offer checks and the transition rules (a conflict is flagged, never applied). A guess (low confidence) and *none* are not suggested: the email is recorded as looked at.
- **The quote has to fit the signal.** It must appear in the email word for word, a quote that plainly says something else (a rejection sentence given for an *offer*) is refused, and an **offer** needs offer wording in its quote (an offer in words the lists do not know is not suggested: it is the one email a scammer most wants believed). For the other signals, wording the lists do not know is allowed: finding those is what the model is for.
- **The email is data, not instructions.** It is sanitised and fenced with a random tag, the system prompt says so, the application's name and title (which come from a scraped posting) are cleaned to one plain line, and an email, a company name or a title that carries instructions aimed at a reader is **not sent to the model at all** (recorded as noise, no call, no cost). Text that the email's own markup hides (`display:none`, `visibility:hidden`, `hidden`, zero-size or zero-opacity text, off-screen boxes, `<head>`, `<template>`, closed `<details>` and the like) is dropped when the email is read, for the rules of 6c as well as the model: the reader sees what a person sees (`scraper/email_html.py`). Matching a browser's error recovery exactly is not possible, so it **fails closed**: when the markup cannot be followed the way a browser would (an end tag a browser would ignore, a never-closed hidden element, nesting deeper than 400 elements, an unterminated comment or tag, more than a bounded amount of work), the email is marked `markup_doubt`: it is **never sent to the model**, and the rules path flags it and caps its confidence at low. Hiding by a `<style>` block is applied for `.class`, `#id` and tag selectors (`@media` rules included); a hiding rule with any other selector raises doubt. When an email has both a plain-text and an HTML part (most mail clients show the HTML), the two are compared: a plain part with more than a few words of its own is `markup_doubt` too. Text hidden only by colour (white on white) cannot be told from visible text; that is one reason the model's answer is checked, capped at medium and decided by a person. Wrongly dropping some visible text (a `font-size:0` wrapper around a child with its own size, `<noscript>` content, a responsive email's hidden-on-mobile block) only means less is read. The parser was compared with a real HTML5 parser on about 130,000 random tag soups during review: no hidden text came through without `markup_doubt`.

**The limits** (docs/runaway-protection.md has the numbers measured by the rehearsal):

| Limit | Value |
|---|---|
| Model calls in one run | at most **10**: a ceiling in the code, so the `mail` schedule's batch of 20 emails still means 10 calls |
| Model calls in one day (the budget's time zone) | at most 40, independent of the money |
| Spending caps | the same monthly and daily caps as scoring and tailoring: the worst case of every call is priced before the email is claimed and again before each call; at a cap nothing is claimed |
| Which emails | only those about a job that can get a reply (the rule of the search's candidates: not scam-blocked, skipped or unshortlisted); an unclear email about any other job is left alone, costs nothing, and is claimed again if the job becomes a candidate again |
| Claims per email | **3**, counted when the email is claimed, then it is **parked**: never asked about again until `gmail rescan` clears it |
| A claim that cost nothing | refunded: the email could not be fetched, Gmail was limiting requests, the Claude API failed or refused, the budget was paused, or the email was settled without a model call (instructions in it, markup that cannot be read, no text). An outage never parks a healthy email. **Not refunded:** a reply the API sent (HTTP 200) but that could not be read, and a failure nothing names: the call may have been billed, so it is logged at the worst case, the claim stays counted, and the email is parked after three |
| A failing API | the first failed call ends the run; nothing is hammered |
| Two runs at once | an email claimed in the last 20 minutes is not claimed again |
| Output | 150 tokens at most, so one call costs about $0.0002 |

So the model step costs at most about **$0.01 a day** (40 calls), and usually nothing: it only sees the emails the rules could not decide. `scripts/jobsearch gmail` shows how many unclear emails wait and how many are parked.

**Rescan and the model.** `gmail rescan` re-opens what the *rules* left aside (unmatched, unclear, rules noise, and emails parked by the model step). It keeps the noise the model already settled, so a rescan never pays twice for the same answer.

**Known limits.** A request the Claude API refuses for a reason tied to one email (an HTTP 400) is refunded and ends the run each time, so it can sit first in the newest-first order and hold the others back at no cost until it changes; the doctor's step (6e) will show a run that keeps ending early. Two runs at the same moment could overshoot the day's limit by one run's calls (the scheduler does not start the stage twice; the money caps still hold either way).

**Stopping points you can see.** The step ends early, and says why, when: the day's calls are used, a spending cap is reached, Gmail or Claude is not answering, or the 240-second deadline passes. The rest waits for the next run.

**Proved by tests.** The unit tests (`scraper/tests/test_mail_ai.py`) run the real Gmail client against the stand-in Gmail, a scripted model and a fake store that follows migration 023, and the SQL tests cover the claims, the lease, refunds, parking, settling and the grants. The rehearsal (`scraper/loop_rehearsal.py`) runs the real SQL and the stand-in APIs: Claude down for 30 hours of runs with 200 emails waiting costs **0** billed calls and parks **0** emails; replies that are never usable cost exactly **3** calls per email and then park it; ten runs back to back inside the lease make **1** call per email; 200 waiting emails with good replies make **40** calls in a day; a daily cap of $0.001 stops at $0.00066; 20 emails that carry instructions make **0** calls. The model step was broken on purpose in 28 ways in the code and 16 in the SQL, and the tests caught every one (one was redundant by design: a second cleaning of the name that the prompt's fence repeats). **Not yet proved against the real Claude API**: the stand-in answers in the format asked for, and the first real run will show whether Haiku sticks to it (the unusable-answer path costs at most three calls per email).

## The scheduled stage (6e)

Deployed 2026-10-13 (`scraper/mail.py`, migration 024). It is what the scheduler's `mail` stage (workflow `06 Check mail`, `POST /mail/run`) does once the Gmail switch is on **and** the Google login is in `.env`:

1. **Check the login** (one profile call). An expired login ends the run and is recorded. A rate limit or an outage (`Unavailable`: a 5xx or a timeout, after one retry) is **not** a login problem: it is recorded as a run that ended early, and the stored login status is left alone, also for a manual `gmail check`. A refusal that will not pass by itself (the API not enabled, an over-broad permission) is stored as a login error and shown by the doctor.
2. **Search the mailbox** ([6c](#the-search-and-the-rules-6c)): at most the `mail` schedule's batch of emails opened per run (default 20: `scripts/jobsearch schedule set mail --batch N`), newest candidates first, nothing opened twice.
3. **Ask the model** about the emails the rules could not decide ([6d](#the-model-step-6d)): at most 10 calls a run and 40 a day, under the spending caps. Skipped, and said so, when `ANTHROPIC_API_KEY` is not set or when the search ended because Gmail was limiting requests (the model step opens emails through Gmail too).
4. **Record what happened**, in counts only (never an email's text), **whatever ends the run**: a login that expires in the middle keeps the counts of what the run did before, and an unexpected failure is recorded as an early run (with only its type) before it is raised: `scripts/jobsearch gmail` shows the last run, and a run counts as *ended early* when Gmail was limiting requests, Claude did not answer or a deadline passed (a day's limit or a spending cap is normal, not early).

The stage aims to fit workflow 06's five-minute timeout (the search gets 120 s, the model step what is left of 240 s but at most 60 s, and one Claude call is cut off after 40 s, retried once). Deadlines are checked between emails, so a very slow answer can still overrun; n8n then gives up while the stage finishes and records its run on its own. A second run is refused while one is going (not queued). It never writes an application's status: it only writes suggestions.

**The doctor** (`scripts/jobsearch doctor`, only while the part is on) warns when the login expired or the last check failed, when the last **three** runs ended early (with the reason), and says how many suggestions wait, when the last run was, and how many emails the model could not read and parked (`gmail rescan` reopens them).

**The Telegram digest** (the message the digest workflow sends, as often as your digest schedule runs it) gains an *Email replies* section: for each **new** suggestion (up to five, then a count) the company and job from your own list, what the reply looks like ("an interview invitation", "a rejection", "an OFFER"...), what it would change, how sure it is, `CHECK THE SENDER` when the email carries a trust flag (look-alike sender, unverified, scam wording, markup that could not be read), and `model-read` when Claude read it. **Never the subject, the sender or the text of the email.** Each suggestion is announced once (`notified_at`), and a section on its own is sent when nothing else is due. If the Google login **expired** the digest says so once, and again only after a working login has expired again. Nothing in the message changes anything.

**Upgrading.** Deploy the scraper first (it works with a database that has not had migration 024: the digest says nothing about mail, the stage records nothing), then `scripts/jobsearch migrate`, then re-import the workflows (`scripts/n8n-import-workflows.sh --publish`: the digest workflow now also passes which suggestions it announced). Do the last two steps back to back: between them the old digest workflow does not mark mail suggestions as announced, so an hourly digest would repeat them.

**Known limits.** Two digests at the same moment (a scheduler tick and *Run on demand*, an n8n retry) can announce the same suggestions twice: the same is true of the job matches. Nothing is lost.

**Proved by tests.** `scraper/tests/test_mail_stage.py` (the order, the counts kept, early runs, every login failure, the timeout budget), the digest and doctor tests, the SQL tests for migration 024, and the rehearsal: one mailbox end to end (a rules suggestion and a model suggestion, each announced once, nothing opened again by five more runs); Gmail answering 429 for 30 runs (30 early runs counted, 0 model calls, reset by one clean run); Claude down for 20 runs (0 billed, 0 parked, the rules' suggestion still made, the unclear email read once Claude is back); an expired login (nothing written, the digest says so once, a working login clears it).

## Adding step 6a to an existing install

The scraper image has to be rebuilt **before** the migration this time, and the new image tolerates the old database (it reports the Gmail switch as off). The other order breaks: the previous scraper image does not know the new `mail` schedule row, so `jobsearch schedule` and the dashboard's Settings page fail until it is rebuilt, and its scheduler would try to start a workflow that is not imported yet.

```bash
git pull
docker compose build scraper && docker compose up -d scraper     # first
scripts/jobsearch migrate                                        # migration 021
scripts/n8n-import-workflows.sh --publish                        # adds workflow 06 Check mail
docker compose --profile dashboard build dashboard && docker compose --profile dashboard up -d --no-deps dashboard   # if the dashboard is on
scripts/jobsearch doctor                                         # no Gmail line until you switch the part on
```

Between the scraper and the dashboard rebuilds the dashboard's Settings page can show an error for a minute (the older dashboard does not know the new fields). Nothing else is affected. To go back, run `scripts/jobsearch features set --gmail off` (the part is off by default anyway); the new tables are harmless to leave.

## Settings

| Setting | Where | Default |
|---|---|---|
| The part on or off | `scripts/jobsearch features set --gmail on\|off` | off |
| How often the stage runs, how many emails one run opens | `scripts/jobsearch schedule set mail --every 30m --batch 20` | every 30 minutes, 20 emails |
| How far back the first search looks | `scripts/jobsearch gmail lookback 30` (or `from-now`) | 60 days |
| The Google login | `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN` in `.env`, written by `scripts/jobsearch gmail login` (a new `.env` has the three blank lines already; `scripts/init-env.sh --add-gmail` adds them to an existing one, and `gmail login` does that for you) | not set |

The dashboard does not show the Gmail switch or the `mail` stage yet: they appear with the inbox (step 6f). The dashboard's API already accepts the switch, so nothing needs changing when they do.

## Privacy

- The scopes are read-only, and the login lives only in the server's `.env`.
- Only matched emails are ever sent to Claude, and only to be classified.
- The database keeps a domain, a subject, a date and a short excerpt per matched email, plus the ids of everything looked at. `scripts/backup.sh` includes them in the database dump, so treat backups as private.
- Nothing in this feature is committed to git: the login is in `.env`, and the emails are in the database.

## Cost

Gmail's API is free at this volume. Reading, matching and the rules cost nothing. A Claude classification of one unclear email is about a fiftieth of a cent at Haiku 5.5's price, under the same monthly and daily caps as everything else. As an estimate (about 1,500 tokens in and 100 out per email), 20 unclear emails a day would be about 12 cents a month. A rules-only mode with no Claude call is planned as a switch. See [costs.md](costs.md).

## Where the ideas come from

The overall flow (search by your companies and the ATS senders, read full bodies, a sourced batch, nothing written before approval, nothing after an offer, idempotent by message id) follows `/gmail-sync` in [MadsLorentzen/ai-job-search](https://github.com/MadsLorentzen/ai-job-search) (MIT). The matching rules (whole-word short names, ignoring placeholder company names) and the sender-authentication check follow [career-ops-hq/career-ops](https://github.com/career-ops-hq/career-ops) (MIT). We reimplement them for this stack rather than copy code; [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) lists what is adapted, and grows as steps 6c and 6d land.
