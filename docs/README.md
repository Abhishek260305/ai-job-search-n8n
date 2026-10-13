# Documentation

Pick the line that matches what you are trying to do.

## Getting started

| | |
|---|---|
| **Have an AI assistant set it up for you** | [setup-with-ai.md](setup-with-ai.md): what the assistant asks you, what only you do, how it builds and checks everything |
| **Install it and get the first results** | [setup.md](setup.md): one linear guide, from nothing to a working pipeline (your computer or a server) |
| **Get a free always-on server** | [vm-oracle.md](vm-oracle.md): create an Oracle Cloud Always Free VM, log in, install Docker |
| **Use it every day** | [daily-use.md](daily-use.md): what runs by itself, what you do, Telegram commands, settings |
| **See everything in a web page** (optional) | [dashboard.md](dashboard.md#open-the-dashboard): turn on the dashboard, open it through the SSH tunnel, or on your phone with Tailscale |
| **Use only some of it** (no Telegram, no tailoring, no Gmail) | [customization.md](customization.md): the three setups, **schedules**, every switch, the fallback for each part |
| **Something is not working** | [troubleshooting.md](troubleshooting.md): symptoms and fixes in one place |
| **Is my spending safe? Can anything loop?** | [runaway-protection.md](runaway-protection.md): every paid path, its limits, the proof, and the emergency brakes |

## Reference

| | |
|---|---|
| [commands.md](commands.md) | the `jobsearch` CLI, the Claude Code slash commands, the Telegram commands, your config files |
| [workflows.md](workflows.md) | the seven n8n workflows (a scheduler plus the pipeline), how to install, run and tune them |
| [gmail.md](gmail.md) | the optional Gmail part (Phase 6): what is built, how it will work, privacy and cost |
| [tailoring.md](tailoring.md) | resume and cover-letter generation, the honesty checks, review, settings |
| [budget.md](budget.md) | spend logging, the hard caps (Claude and Apify), how to set the Anthropic Console limit |
| [runaway-protection.md](runaway-protection.md) | why nothing can loop and burn credits, and how to stop everything fast |
| [testing.md](testing.md) | stand-in APIs, the no-network guard, the runaway rehearsal, the dashboard's tests, and when a real call is justified |
| [costs.md](costs.md) | what each part costs, in one table |
| [sources.md](sources.md) | the job sources: Indeed, Naukri, LinkedIn, Apify, company job boards, remote feeds; how to add companies; coverage and limits |

## How it works inside

| | |
|---|---|
| [architecture.md](architecture.md) | components, the pipeline step by step, the data model |
| [safety.md](safety.md) | scam and prompt-injection defences |
| [dedupe.md](dedupe.md) | how the same job on several boards becomes one |
| [scoring.md](scoring.md) | the rubric, the gates, why the model cannot set its own score |

## Project

| | |
|---|---|
| [roadmap.md](roadmap.md) | what is built, what is next, and the principles we keep |
| [dashboard.md](dashboard.md) | the optional web dashboard: how to turn it on and open it, troubleshooting, security, what each page does (jobs, health, spend, settings, search) and the later versions |
| [adding-a-source.md](adding-a-source.md) | how to add a job board or API |
| [../AGENTS.md](../AGENTS.md) | the working rules for AI assistants in this repo (`CLAUDE.md` imports it) |
| [../THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) | credits for ideas adapted from other projects |

## Words used across the docs

| Term | Meaning |
|---|---|
| **shortlisted** | a job that passed safety and filters and scored at or above your threshold |
| **tailored / drafted** | a resume and cover letter exist for it and wait for your decision |
| **core / optional** | the core is collect → dedupe → filter → score and always runs; tailoring, Telegram and the digest are optional switches ([customization.md](customization.md)) |
| **approved** | you said "ready to apply" (`/approve`), or approval is set to automatic |
| **applied** | you submitted it yourself and told the system (`/applied`) |
| **needs attention** | the honesty checks removed or flagged something; read `summary.md` before using it |
| **cap** | the monthly or daily Claude spend limit; at the cap, paid steps pause |
| **service / scraper** | the Python container that holds all the logic (the folder is still called `scraper/`) |
| **profile.json / resume.json / search.json** | your three config files, kept out of git |

### Statuses

A job moves through: `new` → (`filtered_out` or `scam_blocked`, or) `scored` → `shortlisted` → `tailored` → `approved` → `applied`. It can also be `skipped` (you passed) or `expired` (its deadline went by).

An application moves through: `drafted` → `approved` → `applied`, or `skipped`. The later values (`in_progress`, `interview`, `offer`, `rejected`, `withdrawn`) you set by hand with `scripts/jobsearch mark` (or `/mark`); optional email tracking will also be able to propose them.
