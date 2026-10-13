"""Telegram review bot: commands, replies and PDF delivery. Polling only (no public URL needed).

Security: only messages from the one authorised chat id (TELEGRAM_CHAT_ID) are acted on; everything
else is counted and ignored. The bot token appears only in the API URL path and is never logged or
included in errors. Commands that spend money (/tailor) go through the same budget guard as
everything else.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import httpx

import budget
import tailor_service

HELP = """Job-search bot commands
/review            applications waiting for your decision
/approve 24        approve application 24 (optional note after the number)
/skip 24           skip it
/applied 24        mark it as applied (you submitted it)
/mark 24 interview mark any stage by hand: approved skipped applied in_progress interview offer rejected withdrawn
/tailor 24         tailor the application for job 24 now
/files 24          send the PDFs again
/status            what the pipeline has found
/budget            Claude spend against your caps
/settings          tailoring settings
/set mode auto|manual            tailor automatically, or only when asked
/set approval review|cli|auto    ask me here, ask me only at the command line, or store them ready
/set min_score 70|none           lowest score to tailor (none = your search threshold)
/set max_score 80|none           at or above this the base resume is used as is
/set daily_limit 5|none          most applications tailored per day
/set cover_letter on|off
/set verify on|off              second-layer fact check of the generated text (recommended)
/set max_pages 1|2
/set paper a4|letter             page size of the PDFs
/set style_notes <text>|none     tone and wording preferences for the writing (never lets it add facts)"""

MARK_STATUSES = ("approved", "skipped", "applied", "in_progress", "interview", "offer", "rejected", "withdrawn")

SETTING_PARSERS: dict[str, Callable[[str], object]] = {}


class BotError(RuntimeError):
    pass


@dataclass
class Reply:
    text: str
    documents: list[tuple[str, bytes]] = field(default_factory=list)   # (filename, bytes)


class TelegramClient:
    def __init__(self, token: str, client: Optional[httpx.Client] = None, timeout: float = 40.0):
        if not token:
            raise BotError("TELEGRAM_BOT_TOKEN is not set for the scraper service (add it to .env, then restart it)")
        self._base = f"https://api.telegram.org/bot{token}"
        self.client = client or httpx.Client(timeout=timeout)

    def _call(self, method: str, **kw):
        try:
            r = self.client.post(f"{self._base}/{method}", **kw)
            data = r.json()
        except (httpx.HTTPError, ValueError) as ex:
            raise BotError(f"Telegram {method} failed ({type(ex).__name__})")
        if not data.get("ok"):
            raise BotError(f"Telegram {method} was rejected: {str(data.get('description'))[:160]}")
        return data["result"]

    def get_updates(self, offset: int) -> list[dict]:
        return self._call("getUpdates", json={"offset": offset, "timeout": 0, "allowed_updates": ["message"]})

    def send_message(self, chat_id: int, text: str) -> None:
        for chunk in _chunks(text):
            self._call("sendMessage", json={"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True})

    def send_document(self, chat_id: int, filename: str, data: bytes, caption: str = "") -> None:
        self._call("sendDocument", data={"chat_id": str(chat_id), "caption": caption[:900]},
                   files={"document": (filename, data, "application/pdf")})


def _chunks(text: str, n: int = 3900) -> list[str]:
    return [text[i:i + n] for i in range(0, len(text), n)] or [""]


def _parse_int_or_none(v: str, lo: int, hi: int):
    if v.lower() in ("none", "off", "null", "-"):
        return None
    if not re.fullmatch(r"\d{1,4}", v) or not lo <= int(v) <= hi:
        raise ValueError(f"must be a whole number from {lo} to {hi}, or none")
    return int(v)


def parse_setting(key: str, value: str) -> tuple[str, object]:
    """Validate `/set key value` into a column name and a value. Raises ValueError with a helpful message."""
    key, value = key.lower(), value.strip()
    if key in ("mode",):
        if value.lower() not in ("auto", "manual"):
            raise ValueError("mode must be auto or manual")
        return "mode", value.lower()
    if key == "approval":
        if value.lower() not in ("review", "cli", "auto"):
            raise ValueError("approval must be review, cli or auto")
        return "approval", value.lower()
    if key in ("min_score", "max_score"):
        return key, _parse_int_or_none(value, 0, 101 if key == "max_score" else 100)
    if key == "daily_limit":
        return "daily_limit", _parse_int_or_none(value, 0, 1000)
    if key == "cover_letter":
        if value.lower() in ("on", "true", "yes", "1"):
            return "cover_letter", True
        if value.lower() in ("off", "false", "no", "0"):
            return "cover_letter", False
        raise ValueError("cover_letter must be on or off")
    if key == "verify":
        if value.lower() in ("on", "true", "yes", "1"):
            return "verify", True
        if value.lower() in ("off", "false", "no", "0"):
            return "verify", False
        raise ValueError("verify must be on or off")
    if key == "paper":
        if value.lower() not in ("a4", "letter"):
            raise ValueError("paper must be a4 or letter")
        return "paper", value.lower()
    if key == "style_notes":
        if value.lower() in ("none", "off", "null", "-", ""):
            return "style_notes", None
        clean = " ".join(value.split())
        if len(clean) > 400:
            raise ValueError("style_notes can be at most 400 characters")
        return "style_notes", clean
    if key == "max_pages":
        if value not in ("1", "2"):
            raise ValueError("max_pages must be 1 or 2")
        return "max_pages", int(value)
    for name, choices in (("engine", ("v2",)), ("gap_policy", ("omit", "bridge")), ("cover_style", ("evidence", "classic")),
                          ("tone", ("formal", "direct", "conversational"))):
        if key == name:
            if value.lower() not in choices:
                raise ValueError(f"{name} must be {' or '.join(choices)}")
            return name, value.lower()
    for name in ("reviewer", "company_research"):
        if key == name:
            if value.lower() in ("on", "true", "yes", "1"):
                return name, True
            if value.lower() in ("off", "false", "no", "0"):
                return name, False
            raise ValueError(f"{name} must be on or off")
    raise ValueError(f"unknown setting '{key}' (see /help)")


def normalise_settings(fields: dict) -> dict:
    """Validate a dict of tailoring settings (from the HTTP API or the CLI) using the same rules as /set.
    A value of None means 'none' for the optional numeric settings. Raises ValueError."""
    out: dict = {}
    for key, v in fields.items():
        if key in ("model", "verify_model", "reviewer_model"):
            if not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9._-]{3,80}", v):
                raise ValueError("model must be a model name such as claude-sonnet-5-5")
            out[key] = v
            continue
        text = "none" if v is None else ("on" if v else "off") if isinstance(v, bool) else str(v)
        col, val = parse_setting(key, text)
        out[col] = val
    return out


def settings_text(s: dict) -> str:
    return "\n".join([
        "Tailoring settings",
        f"mode: {s['mode']}  (auto tailors shortlisted jobs by itself; manual waits for /tailor N)",
        f"approval: {s['approval']}  (review asks you here; cli asks only at the command line; auto stores them ready)",
        f"min_score: {s['min_score'] if s['min_score'] is not None else 'your search threshold'}",
        f"max_score: {s['max_score'] if s['max_score'] is not None else 'none'}  (at or above it the base resume is used as is)",
        f"daily_limit: {s['daily_limit'] if s['daily_limit'] is not None else 'none'}",
        f"cover_letter: {'on' if s['cover_letter'] else 'off'}   max_pages: {s['max_pages']}   paper: {s.get('paper', 'a4')}   model: {s['model']}",
        f"style_notes: {s.get('style_notes') or 'none'}",
        f"verify (fact check): {'on' if s.get('verify', True) else 'off'}   ({s.get('verify_model', 'claude-haiku-5-5')})",
        f"engine: v2   gap_policy: {s.get('gap_policy', 'omit')}   "
        f"cover_style: {s.get('cover_style', 'evidence')}   tone: {s.get('tone', 'direct')}",
        f"reviewer: {'on' if s.get('reviewer', True) else 'off'} ({s.get('reviewer_model', 'claude-haiku-5-5')})   company_research: {'on' if s.get('company_research', True) else 'off'}"])


def friendly_names(basics: dict, company: str) -> tuple[str, str]:
    base = f"{tailor_service.slug(basics.get('name'), 30)}"
    co = tailor_service.slug(company, 24)
    return f"{base}_resume_{co}.pdf", f"{base}_cover_letter_{co}.pdf"


class BotHandler:
    def __init__(self, store, tg: TelegramClient, chat_id: int, basics: dict, run_tailor: Callable[[dict], dict]):
        self.store, self.tg, self.chat_id, self.basics, self.run_tailor = store, tg, chat_id, basics, run_tailor

    # ---------------------------------------------------------------- polling

    def poll(self) -> dict:
        offset = self.store.telegram_offset()
        updates = self.tg.get_updates(offset)
        handled = ignored = failed = 0
        for u in updates:
            offset = max(offset, u["update_id"] + 1)
            # At-most-once: move past the update BEFORE acting on it. A command that spends money (/tailor)
            # must never run twice because a slow run overlapped the next poll or the process died midway;
            # if one is lost, the user simply sends it again.
            self.store.set_telegram_offset(offset)
            msg = u.get("message") or {}
            chat = (msg.get("chat") or {}).get("id")
            text = (msg.get("text") or "").strip()
            try:
                if chat != self.chat_id:
                    ignored += 1          # never reply to, or act for, anyone else
                elif text.startswith("/"):
                    handled += 1
                    try:
                        for rep in self.handle(text):
                            self._send(rep)
                    except Exception as ex:   # a crashing command must never wedge the queue
                        failed += 1
                        try:
                            self.tg.send_message(self.chat_id, f"\u26A0 That command failed: {type(ex).__name__}: {str(ex)[:160]}")
                        except Exception:
                            pass
            finally:
                pass
        return {"updates": len(updates), "handled": handled, "ignored": ignored, "failed": failed}

    def _send(self, rep: Reply) -> None:
        self.tg.send_message(self.chat_id, rep.text)
        for name, data in rep.documents:
            self.tg.send_document(self.chat_id, name, data)

    def notify(self, result: dict, job: dict) -> None:
        """Send a finished application for review (message plus PDFs)."""
        self._send(Reply(tailor_service.review_message(result, job), self._files(result.get("files_dir"), job)))

    # ---------------------------------------------------------------- commands

    def handle(self, text: str) -> list[Reply]:
        parts = text.split()
        cmd = parts[0].lower().split("@")[0]
        args = parts[1:]
        try:
            if cmd in ("/start", "/help"):
                return [Reply(HELP)]
            if cmd == "/status":
                return [Reply(self._status())]
            if cmd == "/budget":
                return [Reply(self._budget())]
            if cmd == "/settings":
                return [Reply(settings_text(self.store.tailor_settings()))]
            if cmd == "/set":
                return [self._set(args)]
            if cmd == "/review":
                return [Reply(self._review())]
            if cmd in ("/approve", "/skip", "/applied"):
                return [self._act(cmd[1:], args)]
            if cmd == "/mark":
                return [self._mark(args)]
            if cmd == "/tailor":
                return [self._tailor(args)]
            if cmd == "/files":
                return [self._send_files(args)]
        except (ValueError, LookupError) as ex:
            return [Reply(f"⚠ {ex}")]
        return [Reply("I did not understand that. Send /help for the commands.")]

    @staticmethod
    def _job_id(args: list[str]) -> int:
        if not args or not re.fullmatch(r"\d{1,9}", args[0]):
            raise ValueError("give a job number, for example /approve 24")
        return int(args[0])

    def _status(self) -> str:
        counts = self.store.status_counts()
        st = self.store.budget_status()
        q = len(self.store.review_queue())
        lines = ["Jobs: " + ", ".join(f"{k} {v}" for k, v in counts.items()),
                 f"Waiting for your review: {q}", budget.footer_text(st)]
        alert = budget.alert_text(st)
        return "\n".join(lines + ([alert] if alert else []))

    def _budget(self) -> str:
        st = self.store.budget_status()
        s = budget.summarise(st)
        out = [budget.footer_text(st), f"Today: ${s['day_spent']:.2f} of ${s['daily_cap']:.2f}",
               "Status: " + (f"PAUSED, {s['reason']}" if s["blocked"] else "warning, over the alert level" if s["warn"] else "ok")]
        return "\n".join(out)

    def _review(self) -> str:
        rows = self.store.review_queue()
        if not rows:
            return "Nothing is waiting for your review."
        lines = [f"{len(rows)} waiting:"]
        for r in rows[:15]:
            lines.append(f"#{r['job_id']} {r['score']} {r['title']} @ {r['company']}" + ("  ⚠ needs attention" if r["needs_attention"] else ""))
        lines.append("Use /approve N or /skip N. /files N resends the PDFs.")
        return "\n".join(lines)

    def _set(self, args: list[str]) -> Reply:
        if args and args[0].lower() == "style_notes" and len(args) >= 2:
            args = [args[0], " ".join(args[1:])]       # the one setting whose value is a sentence
        if len(args) != 2:
            raise ValueError("usage: /set <setting> <value>  (see /help)")
        col, val = parse_setting(args[0], args[1])
        try:
            s = self.store.update_tailor_settings(**{col: val})
        except Exception as ex:      # e.g. min_score >= max_score violates the table's check
            raise ValueError(f"that combination was rejected: {str(ex)[:120]}")
        return Reply("✅ updated.\n" + settings_text(s))

    def _act(self, action: str, args: list[str]) -> Reply:
        job_id = self._job_id(args)
        note = " ".join(args[1:]) or None
        try:
            new = self.store.review_application(job_id, action, note)
        except Exception as ex:
            raise ValueError(str(ex).splitlines()[0][:160])
        verb = {"approved": "approved ✅ (ready to apply)", "skipped": "skipped", "applied": "marked as applied \U0001F389"}[new]
        return Reply(f"Job {job_id}: {verb}.")

    def _mark(self, args: list[str]) -> Reply:
        """Set any stage by hand (the Gmail-free way to track replies): /mark 24 interview phone screen Tuesday."""
        job_id = self._job_id(args)
        stage = args[1].lower().replace("-", "_") if len(args) > 1 else ""
        if stage not in MARK_STATUSES:
            raise ValueError("give a stage after the number: " + ", ".join(MARK_STATUSES))
        note = " ".join(args[2:]) or None
        try:
            new = self.store.set_application_status(job_id, stage, note)
        except Exception as ex:
            raise ValueError(str(ex).splitlines()[0][:160])
        return Reply(f"Job {job_id}: marked {new}.")

    def _tailor(self, args: list[str]) -> Reply:
        job_id = self._job_id(args)
        job = self.store.get_job(job_id)
        if not job:
            raise ValueError(f"no job {job_id}")
        if job["status"] in ("scam_blocked", "filtered_out", "expired", "applied"):
            raise ValueError(f"job {job_id} is {job['status']}; I will not tailor it")
        if job.get("score") is None:
            raise ValueError(f"job {job_id} has not been scored yet")
        result = self.run_tailor(job)
        if result["status"] in ("failed", "api_error", "budget_paused"):
            return Reply(f"Could not tailor job {job_id}: {result.get('reason')}")
        return Reply(tailor_service.review_message(result, job), self._files(result.get("files_dir"), job))

    def _send_files(self, args: list[str]) -> Reply:
        job_id = self._job_id(args)
        app = self.store.application(job_id)
        job = self.store.get_job(job_id)
        if not app or not job:
            raise ValueError(f"job {job_id} has no application yet")
        return Reply(f"Files for job {job_id}", self._files(app["files_dir"], job))

    def _files(self, files_dir: Optional[str], job: dict) -> list[tuple[str, bytes]]:
        if not files_dir:
            return []
        rn, cn = friendly_names(self.basics, job.get("company") or "")
        out = []
        for fname, send_as in (("resume.pdf", rn), ("cover_letter.pdf", cn)):
            p = Path(files_dir) / fname
            if p.is_file():
                out.append((send_as, p.read_bytes()))
        return out
