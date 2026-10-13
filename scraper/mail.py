"""Gmail status tracking: the parts that exist so far (Phase 6a, docs/gmail.md).

This step reads no mailbox. It holds what the later steps stand on: the rules for the two settings (how far back the
first search looks, and whether to look back at all), the plain-English report the CLI and doctor show, and the scheduled
stage that the scheduler starts, which for now only says what is missing. Pure functions, no I/O.

Step 6b adds the login check: `check_login` asks Gmail for the account's profile (the cheapest proof the saved login works) and
the result is recorded for the doctor and the CLI. Still no email is read.

The reader never writes an application's status. When it exists it will write *suggestions* (table email_suggestions)
that a person accepts; accepting goes through the same function as a click on the board.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Callable, Mapping, Optional, Union

# The command-line tool imports this module for the settings rules and runs on any machine with only the standard library, so
# the Gmail client (which needs httpx) is imported where it is used, never at the top. tests/test_jobsearch_cli.py proves it.
def _client_module():
    import gmail_client
    return gmail_client


MAX_LOOKBACK_DAYS = 365
DEFAULT_LOOKBACK_DAYS = 60


def validate_settings(fields: Mapping[str, object]) -> dict:
    """The clean column values for a settings change. Raises ValueError with a message for a person."""
    out: dict = {}
    for key, value in fields.items():
        if value is None:
            continue
        if key == "lookback_days":
            if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MAX_LOOKBACK_DAYS:
                raise ValueError(f"lookback must be a whole number of days from 1 to {MAX_LOOKBACK_DAYS}")
            out["lookback_days"] = value
        elif key == "from_now":
            if not isinstance(value, bool):
                raise ValueError("from_now must be true or false")
            out["from_now"] = value
        else:
            raise ValueError(f"unknown Gmail setting '{key}'")
    return out


_ISO = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}(?::?\d{2})?)?$")


def to_datetime(value: Union[str, datetime, None]) -> Optional[datetime]:
    """A timestamp from the report as an aware datetime. gmail_overview() is a jsonb report, so its timestamps arrive as
    ISO strings ("2026-10-12T12:00:00.1234+00:00": Postgres drops trailing zeros, which older Pythons refuse); a datetime
    is accepted too. Anything else is None, never an exception: a report line must not take the command down."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    m = _ISO.match(value.strip()) if isinstance(value, str) else None
    if not m:
        return None
    day, clock, frac, zone = m.groups()
    zone = "+00:00" if zone in (None, "Z") else (zone if ":" in zone else (zone + ":00" if len(zone) == 3 else zone[:3] + ":" + zone[3:]))
    try:
        return datetime.fromisoformat(f"{day}T{clock}.{(frac or '0').ljust(6, '0')[:6]}{zone}")
    except ValueError:
        return None


def _ago(ts: Union[str, datetime, None], now: datetime) -> str:
    ts = to_datetime(ts)
    if ts is None:
        return "never"
    mins = int((now - ts).total_seconds() // 60)
    if mins < 1:
        return "just now"
    if mins < 90:
        return f"{mins} min ago"
    if mins < 2880:
        return f"{mins // 60} h ago"
    return f"{mins // 1440} days ago"


def login_line(overview: Mapping, configured: bool, now: datetime) -> str:
    """One line about the Google login: whether it is set up and whether Google still accepts it."""
    status = overview.get("token_status") or "none"
    checked = overview.get("token_checked_at")
    if not configured:
        return "login:      not set up: scripts/jobsearch gmail login (docs/gmail.md)"
    if status == "ok":
        return f"login:      works (checked {_ago(checked, now)})"
    if status == "expired":
        return "login:      EXPIRED or revoked: sign in again with scripts/jobsearch gmail login"
    if status == "error":
        return f"login:      the last check failed: {overview.get('last_error') or 'unknown error'}"
    return "login:      set in .env, not used yet"


def describe(overview: Mapping, eff: Mapping, configured: bool, now: Optional[datetime] = None) -> list[str]:
    """Lines for `jobsearch gmail`: the switch, the login, where the search stands, and the suggestion counts.
    `eff` is features.effective(...)["gmail"]; `configured` is features.gmail_configured(env)."""
    now = now or datetime.now().astimezone()
    lines = ["gmail:      " + ("on" if eff["on"] else f"off, {eff['reason']}"),
             login_line(overview, configured, now)]
    if overview.get("search_cursor") is None:
        first = ("the first search starts the clock and fetches nothing: only mail that arrives afterwards is looked at"
                 if overview.get("from_now") else f"the first search looks back {overview.get('lookback_days')} days")
        lines.append(f"search:     never run; {first}")
    else:
        lines.append(f"search:     last started {_ago(overview.get('search_cursor'), now)}"
                     f" (a run last finished {_ago(overview.get('last_run_at'), now)})")
    lines.append(f"emails:     {overview.get('messages_seen', 0)} looked at; suggestions: {overview.get('pending', 0)} waiting for you, "
                 f"{overview.get('accepted', 0)} accepted, {overview.get('dismissed', 0)} dismissed")
    summary = overview.get("last_summary")
    if isinstance(summary, Mapping) and summary:
        lines.append("last run:   " + describe_summary(summary))
    early = int(overview.get("early_stops") or 0) if isinstance(overview.get("early_stops"), (int, float)) else 0
    if early >= 2:
        why = _stop_reason(summary)
        lines.append(f"problem:    the last {early} runs ended early" + (f" ({why})" if why else "") + ": mail is being read slowly or not at all")
    unclear = overview.get("unclear") or {}
    if unclear.get("waiting") or unclear.get("parked"):
        line = f"unclear:    {unclear.get('waiting', 0)} wait for a closer look by Claude"
        if unclear.get("parked"):
            line += f", {unclear['parked']} parked (asked about three times without a usable answer; `gmail rescan` reopens them)"
        lines.append(line)
    return lines


def _stop_reason(summary) -> str:
    if not isinstance(summary, Mapping):
        return ""
    if summary.get("stopped"):
        return str(summary["stopped"])[:120]
    for part in ("search", "classify"):
        sub = summary.get(part)
        if isinstance(sub, Mapping) and sub.get("stopped"):
            return str(sub["stopped"])[:120]
    return ""


def _n(value) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def describe_summary(summary: Mapping) -> str:
    """One line for what a run did, from the counts the stage stored (never an email's text). Anything malformed counts as zero."""
    search = summary.get("search") if isinstance(summary.get("search"), Mapping) else {}
    classify = summary.get("classify") if isinstance(summary.get("classify"), Mapping) else {}
    parts = []
    if search:
        opened = _n(search.get("opened"))
        parts.append(f"opened {opened} email{'s' if opened != 1 else ''} ({_n(search.get('suggested'))} suggested by the rules, {_n(search.get('unclear'))} unclear)"
                     + ("" if search.get("complete") else ", more waiting"))
    if classify.get("note"):
        parts.append(f"model step skipped: {str(classify['note'])[:120]}")
    elif classify:
        parts.append(f"the model looked at {_n(classify.get('asked'))} ({_n(classify.get('suggested'))} suggested"
                     + (f", {_n(classify.get('bad_answers'))} unusable" if _n(classify.get("bad_answers")) else "") + ")")
    why = _stop_reason(summary)
    if not parts:
        return "ended early" + (f": {why}" if why else "")
    return "; ".join(parts) + (f"; stopped: {why}" if why else "")


def check_login(client) -> dict:
    """Ask Gmail for the account's profile. -> {"status": ok|expired|error, "account": masked address or "", "message": str}.
    The message never contains a credential (it is redacted) and is what the CLI, the doctor and the dashboard will show."""
    gc = _client_module()
    try:
        profile = client.profile()
    except gc.LoginExpired as e:
        return {"status": "expired", "account": "", "message": gc.redact(e)}
    except gc.GmailError as e:                         # over-broad permission, a rate limit, Google being down, ...
        # a rate limit or an outage passes ("transient") and says nothing about the login; anything else (an over-broad permission, the
        # API not enabled, a refused login, a policy block) will not pass by itself
        return {"status": "error", "account": "", "message": gc.redact(e), "transient": isinstance(e, (gc.RateLimited, gc.Unavailable))}
    return {"status": "ok", "account": gc.mask_address(profile["email"]), "message": "the login works"}


def check(store, env: Mapping[str, str], *, ran: bool = False, make_client: Optional[Callable] = None) -> dict:
    """Check the saved login and record the result (gmail_state). `ran` also stamps last_run_at (the scheduled stage does)."""
    gc = _client_module()
    try:
        client = (make_client or gc.GmailClient.from_env)(env)
    except gc.NotConfigured as e:
        return {"status": "none", "account": "", "message": gc.redact(e)}
    try:
        result = check_login(client)
    finally:
        if hasattr(client, "close"):
            client.close()
    if not (result["status"] == "error" and result.get("transient")):       # a rate limit or an outage is shown to the person, never stored as a bad login
        store.record_gmail_login(result["status"], None if result["status"] == "ok" else result["message"], ran=ran)
    return result


SEARCH_DEADLINE_S = 120.0              # the stage aims to fit workflow 06's five-minute timeout: the search, then the model step
CLASSIFY_DEADLINE_S = 60.0
STAGE_BUDGET_S = 240.0                 # the model step gets what is left of this, at least MIN_CLASSIFY_S
MIN_CLASSIFY_S = 5.0
CLAUDE_TIMEOUT_S = 40.0                # one Claude call, retried once: a hung call cannot take minutes
SEARCH_KEYS = ("opened", "suggested", "noise", "unmatched", "unclear", "failed", "listed", "names_skipped")
CLASSIFY_KEYS = ("claimed", "asked", "suggested", "noise", "bad_answers", "skipped", "released")


def _lazy(name: str):
    import importlib
    return importlib.import_module(name)


def _default_ai(env: Mapping[str, str]):
    """The Claude client, or None when the key is not set (the model step is then skipped: unclear emails wait)."""
    anthropic_client = _lazy("anthropic_client")
    try:
        return anthropic_client.AnthropicClient(env.get("ANTHROPIC_API_KEY", ""), timeout=CLAUDE_TIMEOUT_S, retries=1)
    except anthropic_client.AnthropicError:
        return None


def _counts(source: Mapping, keys) -> dict:
    return {k: int(source.get(k) or 0) for k in keys}


def run_stage(eff: Mapping, store, env: Mapping[str, str], *, batch: int = 20, model: Optional[str] = None,
              make_client: Optional[Callable] = None, make_ai: Optional[Callable] = None) -> dict:
    """The scheduled `mail` stage. While the part is off it does nothing and says why. Otherwise: check the login and record it;
    search the mailbox (step 6c: at most `batch` emails opened); ask the model about the emails the rules could not decide (step
    6d, under its own caps). The answer carries counts only, never an email's text.

    A login Google no longer accepts ends the run and is recorded for the doctor and the digest. Everything else that goes wrong
    (Gmail limiting requests, Claude not answering, a deadline, an unexpected failure) ends the run early, says why, and is counted:
    the doctor reports a run that keeps ending early. Whatever ends a run, what it did so far is recorded.

    `eff` is features.effective(...)["gmail"]."""
    if not eff["on"]:
        return {"skipped": eff["reason"], "checked": 0}
    gc = _client_module()
    try:
        client = (make_client or gc.GmailClient.from_env)(env)
    except gc.NotConfigured as e:
        return {"skipped": gc.redact(e), "checked": 0, "login": "none"}
    started = time.monotonic()
    summary: dict = {}
    early = False
    try:
        result = check_login(client)
        if result["status"] == "error" and result.get("transient"):
            # Gmail limiting requests or being down says nothing about the login: it is an early run, not a login that stopped working
            reason = result["message"][:120]
            store.gmail_record_run({"stopped": reason}, True)
            return {"skipped": reason, "checked": 0, "login": "unchecked", "early": True, "error": reason}
        store.record_gmail_login(result["status"], None if result["status"] == "ok" else result["message"], ran=True)
        if result["status"] != "ok":
            return {"skipped": result["message"], "checked": 0, "login": result["status"]}
        mail_search, mail_ai = _lazy("mail_search"), _lazy("mail_ai")
        try:
            search = mail_search.run_search(client, store, batch=max(1, int(batch)), deadline_s=SEARCH_DEADLINE_S)
            summary["search"] = {**_counts(search, SEARCH_KEYS), "complete": bool(search.get("complete")), "stopped": gc.redact(search.get("stopped") or "")[:120]}
            early = bool(search.get("stopped"))
            ai = (make_ai or _default_ai)(env)
            if ai is None:
                summary["classify"] = {"note": "ANTHROPIC_API_KEY is not set: unclear emails wait"}
            elif "limiting" in (search.get("stopped") or ""):
                summary["classify"] = {"note": "Gmail is limiting requests: the model step waits for the next run"}     # it would only ask Gmail again
            else:
                kwargs = {"model": model} if model else {}
                left = max(MIN_CLASSIFY_S, min(CLASSIFY_DEADLINE_S, STAGE_BUDGET_S - (time.monotonic() - started)))
                classify = mail_ai.run_classify(client, ai, store, deadline_s=left, **kwargs)
                summary["classify"] = {**_counts(classify, CLASSIFY_KEYS), "spent": round(float(classify.get("spent") or 0), 6),
                                       "stopped": gc.redact(classify.get("stopped") or "")[:120]}
                early = early or (bool(classify.get("stopped")) and not classify.get("capped"))
        except (gc.LoginExpired, gc.Overbroad) as e:
            expired = isinstance(e, gc.LoginExpired)
            store.record_gmail_login("expired" if expired else "error", gc.redact(e))
            store.gmail_record_run({**summary, "stopped": gc.redact(e)[:120]}, True)      # what the run did before the login failed is kept
            return {"skipped": gc.redact(e), "checked": 0, "login": "expired" if expired else "error"}
        except Exception as e:                                  # a failure nothing names (the database, a bug): counted as an early run, then raised
            try:
                store.gmail_record_run({**summary, "stopped": f"an unexpected failure ({type(e).__name__})"}, True)
            except Exception:
                pass                                            # never hide the real failure behind a failure to record it
            raise
    finally:
        if hasattr(client, "close"):
            client.close()
    store.gmail_record_run(summary, early)
    search, classify = summary["search"], summary.get("classify", {})
    out = {"checked": search["opened"], "suggested": search["suggested"] + int(classify.get("suggested") or 0), "login": "ok", "early": early, "summary": summary}
    if early:
        out["error"] = search["stopped"] or str(classify.get("stopped") or "")
    return out
