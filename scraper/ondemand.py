"""Tailoring one job on request (dashboard version 3c): the bookkeeping, apart from the HTTP side (app.py).

A tailoring run takes up to a minute and costs money, so a click starts it in the background and the page asks how it
is going. This module is the part that must never go wrong:

  * one run at a time (the tailoring lock already serialises them; this refuses a second start instead of queueing it,
    because a queued run would spend later than the user thought);
  * a job that was just tailored is left alone for a short while, so a double click or a retry cannot pay twice;
  * what a finished run reports to the page: the outcome and the cost, never file paths or the model's reasoning.

State lives in memory, which is enough: a restart of the service forgets finished runs, and a run that was cut off
simply never reports (the files and the application it wrote, if any, are in the database as usual).
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

COOLDOWN_S = 120           # after a run ends, the same job cannot be started again for this long
KEEP = 50                  # finished runs remembered


class Failed(Exception):
    """A run that stopped with something to tell the user. `spent` is what it cost before it stopped (dollars)."""
    def __init__(self, message: str, spent: float = 0.0):
        super().__init__(message)
        self.spent = float(spent or 0.0)


class Refused(Exception):
    """A start that must not happen (another run is going, or this job was just done). The message is shown to the user."""


def clean_result(result: dict) -> dict:
    """The part of a TailorService result the page may see. No paths, no model notes."""
    keep = ("job_id", "status", "variant", "needs_attention", "attention", "cost_usd", "has_cover_letter", "pages", "reason")
    out = {k: result[k] for k in keep if k in result}
    if isinstance(out.get("reason"), str):
        out["reason"] = out["reason"][:300]
    out["attention"] = [str(a)[:300] for a in (result.get("attention") or [])][:10]
    if isinstance(result.get("requirements"), dict):
        out["requirements"] = {str(k): int(v) for k, v in result["requirements"].items() if isinstance(v, (int, float))}
    return out


class OnDemandRuns:
    def __init__(self, clock: Callable[[], float] = time.time, spawn: Optional[Callable[[Callable[[], None]], None]] = None,
                 cooldown: float = COOLDOWN_S):
        self._clock = clock
        self._cooldown = cooldown
        self._spawn = spawn or self._thread
        self._lock = threading.Lock()
        self._runs: dict[int, dict] = {}

    @staticmethod
    def _thread(fn: Callable[[], None]) -> None:
        threading.Thread(target=fn, name="tailor-on-demand", daemon=True).start()

    # ------------------------------------------------------------------------------------------------ asking

    def running(self) -> Optional[int]:
        with self._lock:
            return next((j for j, r in self._runs.items() if r["state"] == "running"), None)

    def refusal(self, job_id: int) -> Optional[str]:
        """Why this job cannot be started now, or None. Used for the estimate as well as the start."""
        with self._lock:
            return self._refusal(job_id)

    def _refusal(self, job_id: int) -> Optional[str]:
        for j, r in self._runs.items():
            if r["state"] == "running":
                return ("this job is being tailored right now" if j == job_id
                        else "another job is being tailored right now: wait for it to finish")
        last = self._runs.get(job_id)
        if last and last["state"] != "running" and self._clock() - last["finished_at"] < self._cooldown:
            wait = int(self._cooldown - (self._clock() - last["finished_at"])) + 1
            return f"this job was tailored a moment ago: wait about {wait} seconds before doing it again"
        return None

    def status(self, job_id: int) -> dict:
        with self._lock:
            r = self._runs.get(job_id)
            if r is None:
                return {"state": "idle"}
            return {k: v for k, v in r.items() if k in ("state", "started_at", "finished_at", "result", "error", "cost_usd")}

    # ------------------------------------------------------------------------------------------------ doing

    def start(self, job_id: int, work: Callable[[], dict]) -> dict:
        """Run `work()` (which returns a TailorService result) in the background. Raises Refused if it must not start."""
        with self._lock:
            why = self._refusal(job_id)
            if why:
                raise Refused(why)
            self._runs[job_id] = {"state": "running", "started_at": self._clock()}
            while len(self._runs) > KEEP:                      # forget the oldest finished ones
                oldest = min((j for j, r in self._runs.items() if r["state"] != "running"),
                             key=lambda j: self._runs[j]["finished_at"], default=None)
                if oldest is None:
                    break
                del self._runs[oldest]

        def run() -> None:
            try:
                result = clean_result(work())
                done = {"state": "done", "result": result}
            except Exception as e:                              # nothing may escape a background thread unreported
                done = {"state": "failed", "error": _plain(e)}
                if getattr(e, "spent", 0):
                    done["cost_usd"] = round(float(e.spent), 6)
            with self._lock:
                self._runs[job_id] = {**self._runs[job_id], **done, "finished_at": self._clock()}

        self._spawn(run)
        return self.status(job_id)


def _plain(e: Exception) -> str:
    """One readable line for the page; never a stack trace, never more than a sentence or two."""
    text = str(e) if isinstance(e, Failed) else getattr(e, "detail", None)      # Failed's message is already written for the user
    if isinstance(text, dict) and isinstance(text.get("errors"), list):
        text = "; ".join(str(x) for x in text["errors"])
    if not isinstance(text, str) or not text:
        text = f"{type(e).__name__}: {e}"
    return " ".join(text.split())[:300]
