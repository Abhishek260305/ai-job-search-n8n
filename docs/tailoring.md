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
