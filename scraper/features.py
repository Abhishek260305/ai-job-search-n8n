"""Which optional parts of the pipeline are in use.

Collecting, filtering and scoring are the core. Everything after that is optional:

  tailoring   resume + cover-letter generation (a master switch; off = the pipeline stops at scoring)
  telegram    review and commands from your phone (also needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
  digest      the morning message listing new matches (needs Telegram)
  gmail       suggest status changes from the replies in your Gmail (also needs a Google login in .env; OFF until you ask)

The switches live in the database (`feature_settings`), so they change instantly and survive restarts.
`effective()` combines a switch with what is actually configured, and says why a part is off, so the
CLI, the doctor and the Telegram bot all describe the same state. Pure functions, no I/O.
"""
from __future__ import annotations

import re
from typing import Mapping, Optional

FEATURES = ("tailoring", "telegram", "digest", "gmail")
APPROVALS = ("review", "cli", "auto")

TELEGRAM_VARS = "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID"
GMAIL_VARS = ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN")


def telegram_configured(env: Mapping[str, str]) -> bool:
    """A bot token and a numeric chat id are both present."""
    return bool((env.get("TELEGRAM_BOT_TOKEN") or "").strip()) and bool(re.fullmatch(r"-?\d+", (env.get("TELEGRAM_CHAT_ID") or "").strip()))


def gmail_configured(env: Mapping[str, str]) -> bool:
    """The three values of a Google login (client id, client secret, refresh token) are all present."""
    return all((env.get(name) or "").strip() for name in GMAIL_VARS)


def effective(flags: Mapping[str, bool], env: Mapping[str, str]) -> dict:
    """-> {feature: {"on": bool, "reason": str}} for tailoring, telegram, digest, gmail.
    `reason` is empty when the part is on, and otherwise says what would turn it on."""
    out: dict = {}
    out["tailoring"] = {"on": True, "reason": ""} if flags.get("tailoring", True) else \
        {"on": False, "reason": "switched off (jobsearch features set --tailoring on)"}

    if not flags.get("telegram", True):
        tg = {"on": False, "reason": "switched off (jobsearch features set --telegram on)"}
    elif not telegram_configured(env):
        tg = {"on": False, "reason": f"not configured: add {TELEGRAM_VARS} to .env (optional)"}
    else:
        tg = {"on": True, "reason": ""}
    out["telegram"] = tg

    if not flags.get("digest", True):
        out["digest"] = {"on": False, "reason": "switched off (jobsearch features set --digest on)"}
    elif not tg["on"]:
        out["digest"] = {"on": False, "reason": "needs Telegram"}
    else:
        out["digest"] = {"on": True, "reason": ""}

    # unlike the three above, a missing flag means OFF: Gmail is only ever used when it was asked for
    if not flags.get("gmail", False):
        out["gmail"] = {"on": False, "reason": "switched off (jobsearch features set --gmail on)"}
    elif not gmail_configured(env):
        out["gmail"] = {"on": False, "reason": f"not configured: add {', '.join(GMAIL_VARS)} to .env (optional, see docs/gmail.md)"}
    else:
        out["gmail"] = {"on": True, "reason": ""}
    return out


def effective_approval(approval: str, telegram_on: bool) -> str:
    """What `approval` means right now.

    review  push each application to Telegram and wait for a decision
    cli     no message; it waits as a draft until you approve it at the command line
    auto    stored as ready (applications the checks flagged still wait for a human)

    `review` needs a way to reach you. Without Telegram it falls back to `auto`: applications are stored
    ready, with nothing to wait for. Choose `cli` to keep a human decision without Telegram."""
    if approval not in APPROVALS:
        raise ValueError(f"approval must be one of {', '.join(APPROVALS)}")
    return "auto" if approval == "review" and not telegram_on else approval


def should_notify(approval: str) -> bool:
    """Whether a drafted application is pushed to Telegram (only `cli` means never)."""
    return approval != "cli"


def describe(flags: Mapping[str, bool], env: Mapping[str, str], approval: Optional[str] = None) -> list[str]:
    """Lines for the CLI and doctor: one per optional part, plus how approval currently behaves."""
    eff = effective(flags, env)
    lines = ["scoring:    always on (collect → dedupe → filter → score is the core)"]
    for name in FEATURES:
        e = eff[name]
        lines.append(f"{name + ':':<11} " + ("on" if e["on"] else f"off, {e['reason']}"))
    if approval:
        now = effective_approval(approval, eff["telegram"]["on"])
        note = f"{approval}" if now == approval else f"{approval} → behaves as {now}: with no Telegram, applications are stored ready"
        lines.append(f"approval:   {note}")
    return lines
