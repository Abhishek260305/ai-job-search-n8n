# Tailoring

For a job worth applying to, the pipeline drafts a resume patch and a cover letter, checks them against your real resume, renders PDFs, stores them, and (if you want) asks you before anything is marked ready. **Tailoring is optional**: switch it off (`scripts/jobsearch features set --tailoring off`) and the pipeline stops at scoring; see [customization.md](customization.md). **There is no auto-apply**: you submit applications yourself.

```
shortlisted job ─► draft (Sonnet) ─► word audit ─► [revise once] ─► repair ─► fact check (Haiku) ─► render ─► verify PDF ─► save ─► you
                   resume patch      numbers,        only if the     remove      claims vs the       HTML→PDF    pages + text     files    approve / skip
                   + cover letter    technologies,   audit failed    whatever    resume              (Gotenberg) layer           + DB
                                     years, style                    is left
```

## What gets produced

Each application gets a folder (`output/applications/<job id>-<company>-<title>/`, on the host next to `docker-compose.yml`):

| File | What it is |
|---|---|
| `resume.pdf` | the tailored (or base) resume, one page by default |
| `cover_letter.pdf` | a specific, forward-looking letter (optional, on by default) |
| `summary.md` | requirement coverage (matched / bridged / gap), every change to your resume, what the honesty checks removed and why, things worth double-checking |
| `patch.json`, `tailored_resume.json` | exactly what changed, and the resume as rendered |
| `audit.json`, `posting.txt` | the audit result and the posting text as it was when tailored |

The model works on a **patch** over your `config/resume.json`: it may reorder bullets, hide the ones that don't help, reword a bullet using the posting's terms, reorder skill groups and emphasise existing keywords, pick a short headline and summary, and write the cover letter. It may **not** add anything. Your bullets keep their ids, so every change is traceable.

## The honesty layers

A tailored resume that claims things you didn't do is worse than none. Four layers stand between the model and the page. (Other pages say "two honesty layers": they count only the two checks, the deterministic audit with its revision and repair, and the Haiku fact check. The prompt is the first of the four, the guard that comes before any check.)

1. **The prompt** forbids inventing anything and spells out the allowed operations. The posting is fenced as untrusted data, so text inside it cannot give orders.
2. **A deterministic audit** (free, instant) compares every generated text with your resume:
   - a number not in your resume is rejected; a *rewrite* may keep only the numbers of its own bullet (no borrowing a metric from another bullet);
   - a technology or proper name not in your resume is rejected; a rewrite may only use words from its own role;
   - a claimed number of years of experience above what your profile supports is rejected;
   - terms that appear only in the posting are a *soft* finding in a cover letter ("your Kafka pipelines" is fine as the employer's own stack) and a hard one in a resume bullet;
   - clichés ("passionate", "leverage", "hit the ground running", ...) are flagged; em/en dashes are replaced.
3. **One revision.** If the audit rejects the draft, the model gets exactly what was rejected and tries once more. Otherwise there is no second paid call.
4. **Repair**, then a **fact check**. Whatever is still unsupported is removed (the original bullet is restored, an unsupported summary is dropped, offending cover-letter sentences are cut). Then a cheap model (Claude Haiku) reads the generated text against your resume for what a word check cannot see: a skill presented as "production experience" when your resume lists it only as a skill, an overstated scale or ownership. Claims it marks **unsupported** are removed; ones it marks **exaggerated** are kept and shown to you; anything it lists but says is supported is ignored. If the fact check cannot run (outage, budget), the application is flagged for a human instead of passing unchecked.

Anything removed is recorded in `summary.md` and the Telegram message, and flags the application "needs attention", which also withholds auto-approval.

What this does **not** guarantee: the checks are strong, not perfect. A model can still phrase something misleadingly in a way no word list or second model catches. Read the documents before you send them.

## Tailoring v2 and comparing the engines

Tailoring has two writers. **v1** is what has always run and is still the default. **v2** is a newer one that writes sharper letters with the same honesty: it can say more of what is true, and it is never allowed to say anything that is not. You choose the engine, after comparing them on your own jobs.

```bash
scripts/jobsearch tailor-compare 24 31 40                 # both engines, side by side, nothing saved as an application
scripts/jobsearch tailor-settings set --engine v2          # switch (needs migration 026: scripts/jobsearch migrate)
scripts/jobsearch tailor-settings set --engine v1          # and back, any time
```

`tailor-compare` writes, per job, `output/compare/<job>/v1/`, `.../v2/` (the usual files) and a `compare.md` with the two cover letters, headlines and summaries, reworded bullets, requirement coverage, keyword coverage and what the checks found, with the cost of each. It **saves no application, changes no status, sends no message and touches no attempt counter**: you can run it on any job (even one you applied to). It costs a draft per engine (about 5 cents each, priced against your caps first, logged as `tailor_compare` so the Spend page shows it apart). It refuses, having spent nothing, while tailoring is off, while paused, for a scam-blocked job, or while a scheduled run is going. `--engines v2` runs one engine.

**The line does not move.** v2 may emphasise, order, word, quantify and show interest. It never adds an employer, title, date, number, technology or skill that the resume, the profile or your proof points do not support, and never words a skill above the level you gave it. Everything below only makes the audit say no more often, or lets the writing use more of what is true.

### What v2 does

| | |
|---|---|
| **Gap policy** (`gap_policy`) | The requirements table is private notes for you. The letter and the summary never state, hint at or apologise for anything you lack, never call you junior or new, never say how long you have worked, and never mention a requirement marked *gap* (nor claim it). `omit` (the default) says nothing; `bridge` adds one "My closest work is..." sentence per important gap, from what you really did. A hedge detector in code ("I should be upfront", "I have not yet", "limited experience", "about a year of professional experience", ...) gives the model one revision and then removes the sentence. Gaps and the closest real work are listed in `summary.md` under "Gaps and bridges", for your interview. |
| **Proof points** (`config/proof_points.json`) | Optional, gitignored, yours: the facts behind a resume bullet (project, what you did, your role `owned/led/built/contributed/used`, technologies, a metric and **where it can be shown**). A number or metric without evidence is refused: no evidence, no number. The letter may use them, and a rewrite of a bullet may use the proof points of *that* bullet (a metric cannot move to another bullet). Copy `config/proof_points.example.json`, then `scripts/jobsearch validate`. Nothing is written for you: an entry exists only if you wrote it. |
| **Skill levels** | Your profile's strong / moderate / weak skills decide the wording: only a *strong* skill may be called proficient or expert; a moderate one is "experience with"; a weak one "familiar with". A skill the resume only **lists** (no bullet shows it) is toolkit, not work done: "my work with Kubernetes" is rejected for it. A skill on the resume that your profile does not rank counts as *moderate* (not ranked is not strong). You raise a skill by editing the profile, not by asking the model. |
| **Ownership and titles** | A rewrite may not use a stronger verb than its bullet (used < contributed < built < led < owned); a letter or summary may not claim "led", "architected" or "owned" if no bullet shows it (a proof point lifts that only for a sentence about *its own* project or technologies; a sentence about the employer is not a claim about you); a title word that is not yours (Senior, Lead, Staff, ...) in the headline, the summary or "I am a ..." is rejected. |
| **No filler** | A rewrite that only appends generic benefit words ("..., improving reliability and cost control") is sent back to the original bullet. |
| **A reviewer pass** (`reviewer`, `reviewer_model`) | One more cheap call (Haiku) reads the draft like a hiring manager and proposes up to six edits. **The code decides:** an edit is kept only if the quoted text is really there, it adds no new line and no look-alike letter, **every word it adds already appears in the text, your resume, your profile, your proof points or the job's title and employer** (so it can tighten, not invent: "doubling throughput" or "3 million users" are refused even when the posting says them), and the whole result gets no finding it did not already have. On a resume used as written, only the letter can change. What was applied and what was set aside is in `summary.md`. |
| **A company angle** (`company_research`) | Up to three plain facts from the employer's **own** website, quoted from its home page and cached 30 days in `company_notes`. The website comes only from the job's own links (a company-board slug or a company-site apply link; never from text in the posting; job boards and applicant-tracking systems do not count), one polite request, no login. A fact is kept only if its quote is really on the page, **every word of it is on the page**, it holds no number, address or "candidate/you/I", and the letter may use at most one; the words of a fact count as the *employer's* (they may be named as its own, never as your experience). The site must end in a usual domain ending, its name must resolve to a public address, only the same host (or its `www.`) is followed on a redirect, and the whole read has a 12-second limit. Most jobs found on LinkedIn have no such link, and then there is no company angle: that is normal. |
| **The letter** (`cover_style`, `tone`) | `evidence` (the default for v2): an opening that names the employer and the role, two to four achievement lines (each with a short lead, and the numbers exactly as your resume or a proof point states them), and a last paragraph on what you would work on first; 300 to 380 words. `classic` keeps the three paragraphs. `tone` is `formal`, `direct` (default) or `conversational`. Cliches (passionate, leverage, robust, seamless, "proven track record", "I am excited to", ...) are avoided and flagged. The letter never names a city or country. |
| **Keywords and the page** | `summary.md` lists the posting's terms three ways: said in the tailored text; in your resume but only in a list or a hidden bullet (code then emphasises them in the skills); and technical words the resume does not have at all (gaps, never stuffed in). If the page would need a type size below 92%, the least relevant bullets of the *older* roles are left out first (never the main role, never a role's last bullet), up to three, before the type shrinks further. |
| **Smaller fixes (both engines)** | A sentence that points back at a removed one ("That is why...") is removed with it. A requirement the model marked matched or bridged without citing a real bullet is shown as a *gap* instead of failing the whole answer. A requirement may cite `education` (a degree) or `skills` (listed only: bridged at most). |

### Cost and safety

v2 adds up to three small calls: the reviewer (about a cent), the company note (a fraction of a cent, once a month per company) and, as before, the fact check. A real comparison of six jobs cost about 6 to 7 cents per job per engine, v2 slightly less than v1 because it needs fewer revisions. Every call is priced at its worst case first, logged under its own purpose (`tailor_review`, `tailor_company`, `tailor_verify`, and `tailor_compare...` for comparisons), counted against the same daily and monthly caps and stopped by the pause; none of it touches a Claude subscription. If the reviewer or the company note cannot run, tailoring goes on without it. The settings are in the database and are changed with the CLI only (the dashboard cannot change the engine, like the model and the approval mode).

What v2 will not do: add a skill, tool, employer, title, date, degree, number or year you do not have; word a skill above its level; use a metric that has no evidence; use a company fact it did not find on the company's own site; hide a gap from *you* (gaps stay in the table, `summary.md` and the Telegram note).

## Settings

Stored in the database; change them any time. Your defaults are `mode auto`, `approval review`.

| Setting | Values | Meaning |
|---|---|---|
| `mode` | `auto` / `manual` | `auto`: shortlisted jobs are tailored by the scheduled workflow. `manual`: only when you ask (`/tailor 24`). |
| `approval` | `review` / `cli` / `auto` | `review`: each application is sent to you on Telegram (message plus PDFs) and waits for `/approve` or `/skip`. `cli`: no message; it waits until you `approve` or `skip` at the command line. `auto`: stored as approved (ready to apply) with no message, unless the checks flagged it. Without Telegram, `review` behaves as `auto` (nobody to ask); pick `cli` to keep a human decision. |
| `min_score` | number / none | lowest score to tailor. None means your search threshold. |
| `max_score` | number / none | at or above this score your base resume is already close, so it is used **as written** (only the cover letter is written). None means never. |
| `daily_limit` | number / none | most applications per day. None means no limit (the spending caps still apply). |
| `cover_letter` | on / off | write a cover letter. |
| `max_pages` | 1 / 2 | the resume is shrunk step by step until it fits; if it cannot, it is flagged. |
| `paper` | `a4` / `letter` | the page size of the resume and cover letter PDFs. Default `a4`. |
| `style_notes` | text, at most 400 characters, or none | your tone and wording preferences for the writing, for example "Warm but formal. Short sentences." They are placed in the prompt as preferences that cannot override the honesty rules, the output format or the writing rules, and the audit and fact check run as always, so they can never make the writing add a claim your resume does not support. |
| `verify` | on / off | the Haiku fact check (recommended). |
| `model` | model id | default `claude-sonnet-5-5`. |
| `engine` | `v1` / `v2` | the writer (see above). Default `v1`. |
| `gap_policy` | `omit` / `bridge` | v2 only. Default `omit`. |
| `cover_style` | `evidence` / `classic` | v2 only. Default `evidence`. |
| `tone` | `formal` / `direct` / `conversational` | v2 only. Default `direct`. |
| `reviewer`, `reviewer_model` | on / off, model id | v2 only: the reviewer pass. Default on, Haiku. |
| `company_research` | on / off | v2 only: the company angle. Default on. |

```bash
scripts/jobsearch tailor-settings                                    # show
scripts/jobsearch tailor-settings set --approval auto --max-score 80 # change
```
or in Telegram: `/settings`, `/set approval auto`, `/set max_score 80`, `/set daily_limit none`.

## Reviewing: Telegram and the CLI

Both update the same records. **Telegram is optional**: without it, use the CLI column, and your files are in `output/applications/`. A decision can be recorded for any job, tailored or not, and `mark` sets the later stages (interview, offer, rejected, ...) by hand.

| Telegram | CLI | |
|---|---|---|
| `/review` | `scripts/jobsearch review` | what is waiting |
| `/approve 24` | `scripts/jobsearch approve 24` | ready to apply |
| `/skip 24` | `scripts/jobsearch skip 24` | not interested (can be approved later) |
| `/applied 24` | `scripts/jobsearch applied 24` | you submitted it |
| `/mark 24 interview` | `scripts/jobsearch mark 24 interview` | any later stage, by hand (email tracking will be an optional extra) |
| `/tailor 24` | `scripts/jobsearch tailor 24` | tailor job 24 now, any scored job |
| `/files 24` | `scripts/jobsearch files 24` | the PDFs again / where they are |

The Telegram bot (if you use it) only acts on messages from **your** chat id; everyone else is ignored. It polls every minute (no public URL needed), and acknowledges a command *before* running it, so a slow `/tailor` can never run twice.

## Cost (measured)

On Claude Sonnet 5.5 with prices from Anthropic's pricing page: a draft plus one revision cost **about $0.048** on the first real run (a draft alone is roughly $0.025). The Haiku fact check adds roughly a tenth of a cent. Every call is priced at its worst case against your caps before it runs, logged afterwards, and a cap pauses tailoring cleanly (see [budget.md](budget.md)). With `max_score` set, jobs above it cost only a cover letter.

## Format and layout

The resume is built as HTML and printed to PDF by [Gotenberg](https://gotenberg.dev) (headless Chromium, one internal container). A real text layer means an applicant-tracking system can read it. After rendering, the PDF is read back: page count, that your name and email are in the text layer, and that there is real extractable text. The layout is shrunk in steps (a binary search for the largest size that fits) until it fits `max_pages`. Cover letters must fit one page.

## Your data

Tailoring sends your `resume.json`, your profile summary and the job posting to the Anthropic API. The finished PDFs are written to `output/` on your server and, in review mode, sent to your own Telegram chat. Nothing is sent anywhere else. `output/` is gitignored.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `/tailor` says "resume not found" | run `/setup`, or put a valid `config/resume.json` in place; `scripts/jobsearch doctor` checks it |
| "ANTHROPIC_API_KEY is not set" | add it to `.env` and `docker compose up -d scraper` |
| the PDF service is unreachable | `docker compose ps` should list `gotenberg` as healthy; `docker compose logs gotenberg` |
| every application says "needs attention" | open `summary.md`: it lists what was removed and why; if the same thing keeps appearing, add the missing fact to `resume.json` |
| the resume spills to 2 pages | lower `max_pages` only if you accept 2; otherwise hide bullets in your base resume |
| a model rejects the request | models differ in how "no thinking" is requested; the client adapts automatically and remembers (see budget.md) |
