# Third-party notices

## MadsLorentzen/ai-job-search

This project's safety checks and parts of its planned scoring, tailoring and email-tracking design
are adapted from ideas in [MadsLorentzen/ai-job-search](https://github.com/MadsLorentzen/ai-job-search),
a Claude Code job-application workflow released under the MIT License. That project is independent of
this one; it is not affiliated with or endorsing it.

What we adapt, in our own implementation:

| Idea | Where it lands here |
|---|---|
| Treat posting text as untrusted data; verify the source host against known boards and official ATS domains, failing closed on look-alikes | `scraper/safety.py`, `docs/safety.md` |
| Four-dimension fit scoring with eligibility, language and location gates | `scraper/scoring.py`, `docs/scoring.md` |
| Onboarding from a CV and LinkedIn export, surfacing source conflicts for the user to decide, and writing confirmed facts back to the profile | `.claude/commands/setup.md`, `docs/commands.md` |
| Drafter/reviewer tailoring with a factual grounding audit and requirement coverage | `scraper/tailor.py`, `scraper/tailor_service.py`, `docs/tailoring.md` |
| Writing-style rules for cover letters (no dashes, no cliches, forward-looking, specific) | `scraper/tailor.py` prompt and audit |
| PDF text-layer and page-count verification before a document is sent | `scraper/render.py` |
| The email sync flow: search by your companies and the ATS senders, read full bodies, a sourced batch, nothing written before approval, idempotent by message id | Phase 6: `docs/gmail.md`, `scraper/gmail_client.py` (more in steps 6c to 6f) |

Where we copy code or substantial text verbatim, the file says so in its header and keeps this notice.

### License text (MIT)

```
MIT License

Copyright (c) 2026 Mads Lorentzen

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## career-ops-hq/career-ops

The Gmail status tracking (Phase 6) also adapts ideas from [career-ops-hq/career-ops](https://github.com/career-ops-hq/career-ops),
an independent MIT-licensed AI job-search tool. We read its approach and write our own code for this stack; nothing is copied
verbatim. It is not affiliated with or endorsing this project.

| Idea | Where it lands here |
|---|---|
| Trust only the authentication verdict (SPF, DKIM, DMARC) that the receiving mail server itself recorded, never a header the sender wrote | `scraper/gmail_client.py` (`parse_auth_results`) |
| Match a reply to an application by whole-word company names (a short name such as "HP" must not match "PHP") and ignore placeholder company names | `scraper/mail_match.py` |
| Classify replies as interview, rejection, offer, acknowledgement or noise by wording, and keep a manual paste path for people without Gmail | `scraper/mail_rules.py`; the paste path is planned for step 6g |

### License text (MIT)

```
MIT License

Copyright (c) 2026 Santiago Fernández de Valderrama

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Runtime dependencies

Installed from their own package indexes, not vendored here: [JobSpy](https://github.com/speedyapply/JobSpy) (MIT), FastAPI, Uvicorn, HTTPX, n8n (Sustainable Use License, self-hosted use), PostgreSQL. Check each project's license for your own use, especially n8n's if you plan to offer this as a service.

## Dashboard front end

The web dashboard (`dashboard/web`) uses:

- **shadcn/ui** (MIT): the files in `dashboard/web/src/components/ui/` were generated from shadcn/ui's component recipes and then edited. shadcn/ui is released under the MIT License by shadcn.
- **Radix UI** (MIT) and **Lucide** icons (ISC), installed as packages: `radix-ui`, `lucide-react`.
- **Geist** font (SIL Open Font License 1.1), installed as the package `@fontsource-variable/geist`.
- React, Vite, Tailwind CSS, Recharts and the other packages listed in `dashboard/web/package.json`, each under its own (permissive) license.

They are installed by `npm` at build time; only the shadcn/ui-derived component source is part of this repository.
