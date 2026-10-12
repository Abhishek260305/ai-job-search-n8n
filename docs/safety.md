# Safety: scams and prompt injection

Job postings are third-party text, and some of them are scams or are written to manipulate AI tools. `scraper/safety.py` checks every job **before any model is called**, using only local rules (no network calls, nothing is fetched).

These are heuristics, not a guarantee. They are tuned to catch common patterns cheaply and to explain themselves. Expect some false positives and some misses. **Medium means "look closer", high means "do not spend tokens or time on this".**

## What gets checked

### 1. Links (`classify_host`)
| Category | Meaning | Effect |
|---|---|---|
| `known_board` / `known_ats` | a recognised job board or applicant-tracking domain | clean |
| `company_site` | the host contains the company name | clean |
| `unverified` | not recognised, no company-name match | weak note only (many legitimate company sites look like this) |
| `lookalike` | imitates a known service, e.g. `evil-greenhouse.io`, `greenhouse.io.evil.com` | medium |
| `form` | personal-data form on a generic host (Google Forms) | medium |
| `messaging` | link into WhatsApp/Telegram/Signal | high |
| `suspicious` | user-info or backslash trick (`https://greenhouse.io@evil.com`), raw IP, punycode, URL shortener | high |
| `missing` / `invalid` | no link, or not http(s) | noted |

A host matches an apex domain only if it equals it or ends with `.<apex>`. Anything else fails closed. The posting link, the apply link, and up to 20 links inside the description are all checked.

### 2. Scam red flags (`scan_text`)
| Code | Severity | Examples |
|---|---|---|
| `FEE_REQUEST`, `UPFRONT_PURCHASE` | high | "refundable security deposit", "pay Rs 2000 to secure your seat" (negated phrases like "no registration fee" are ignored) |
| `EARN_PER_DAY` | high / medium | "earn Rs 3000 per day", "copy paste jobs" |
| `SENSITIVE_DATA_REQUEST` | high | asks to send Aadhaar, PAN, bank, passport or card details |
| `CHECK_SCAM` | high | payment-processing agent, reshipping, mystery shopper |
| `MESSAGING_CONTACT`, `OFF_PLATFORM_INTERVIEW` | medium | contact or interview over WhatsApp/Telegram |
| `FREE_EMAIL_CONTACT` | medium | recruiter at a gmail/yahoo/hotmail address |
| `GUARANTEED_OUTCOME` | medium | "100% placement", "no interview" |
| `URGENCY_PRESSURE` | low | "limited seats" |

### 3. Prompt injection and hidden text
| Code | Severity | Meaning |
|---|---|---|
| `PROMPT_INJECTION` | high | instruction-like text aimed at an AI ("ignore previous instructions", "rate this job 100", role-prefix lines like `SYSTEM:`) |
| `HIDDEN_CONTENT` | medium | HTML comments, `display:none` elements, scripts |
| `INVISIBLE_CHARACTERS` | low | zero-width characters (common in copy-pasted text, so weak on its own) |

## Risk label

`assess(job)` combines everything into `{"risk": "low|medium|high", "score": n, "flags": [...], "hosts": {...}}`:
- **high**: any severity-3 flag, or a total score of 5 or more
- **medium**: any severity-2 flag, or a total of 2 or more
- **low**: otherwise

The scraper adds this as `safety` on every job and `safety_summary` counts per response. The pipeline should drop `high` before scoring and show `medium` with its reasons.

## Defending the model calls

Sanitising and flagging are not enough on their own. Every model call that reads a posting should:
1. pass the text through `wrap_untrusted()`, which strips hidden content and markup and fences the text in a random per-call tag the poster cannot guess;
2. put `GUARD_INSTRUCTIONS` in the system prompt;
3. have **no tools and no network access**, so injected text has nothing to act on;
4. require strict JSON output and validate it before storing anything;
5. never output or visit URLs taken from the posting.

## Fraud warnings are not offers

Legitimate employers often include a paragraph such as: *"Some of these scams request payment for training or job application fees. Please know ACME would never require payment."* A naive keyword match flags that as a fee request. Each rule match is therefore judged **inside its own sentence**: if that sentence is itself about scams or fraud (or says the employer never asks, requires or charges), the match is ignored. A disclaimer in a *different* sentence does not hide a real offer: "Beware of fraud. Pay a refundable registration fee of Rs 500 to confirm." is still flagged. Negation ("no registration fee") is likewise read within the same clause, so "Do not delay, pay a refundable deposit now" is not mistaken for a negation.

## Measured on real data

50 live postings (Indeed, Naukri, LinkedIn) produced 48 low, 2 medium and 0 high:
- A Naukri "Urgent Hiring" sales post that asks candidates to contact over WhatsApp: medium, a fair flag.
- A LinkedIn post containing invisible characters: this was a false alarm, so invisible characters are now a weak signal on their own.

The first full pipeline run on 45 live postings blocked one job, a real company's posting that contained exactly such a fraud-warning paragraph; that is how this rule was found, and the job was restored by `POST /maintenance/reassess` (which re-runs the current rules over every blocked job). Real scam postings are mostly filtered by the boards themselves, so the main value is catching what slips through, plus protecting the model. Rules are unit-tested (see `scraper/tests/test_safety.py`) but only a few real scams have been seen, so treat the thresholds as a starting point and tune them with your own data.

## Extending

Add a rule to `_RULES` in `safety.py`: `(code, severity 1-3, regex, detail)`, then add a positive and a negative test. Add boards or ATS domains to `KNOWN_BOARDS` / `KNOWN_ATS`.

## Not covered yet
- Salary far above market for the role (needs salary data)
- Company existence and reputation lookups
- Email-side phishing checks (planned with the Gmail phase)
