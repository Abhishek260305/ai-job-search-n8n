"""Minimal Anthropic Messages API client (httpx). The key is read from the environment by the caller
and only ever sent in a header; it never appears in logs, errors or return values.

Used for tailoring, where the whole draft -> audit -> revise loop runs inside one service call.
(Scoring still goes through n8n.) Every call's usage is returned so the caller can log it against
the budget (see budget.py).
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Callable, Optional

import httpx

# ANTHROPIC_API_URL points the client at a stand-in server (scraper/stubs.py) for tests and rehearsals. Never set it in production.
API_URL = os.environ.get("ANTHROPIC_API_URL", "https://api.anthropic.com/v1/messages")
RETRY_STATUSES = {429, 500, 502, 503, 529}


class AnthropicError(RuntimeError):
    def __init__(self, message: str, status: Optional[int] = None, retryable: bool = False):
        super().__init__(message)
        self.status, self.retryable = status, retryable


@dataclass
class Reply:
    text: str
    usage: dict
    stop_reason: Optional[str]


# How to ask a model not to think. Models differ: Claude Haiku 5.5 accepts {"type": "disabled"};
# Claude Sonnet 5.5 rejects that and wants {"type": "between_tools"}. We try them in order, and remember
# which one worked for each model. None means "send no thinking setting at all".
THINKING_TRIES = [{"type": "disabled"}, {"type": "between_tools"}, None]


class AnthropicClient:
    def __init__(self, api_key: str, client: Optional[httpx.Client] = None, *, timeout: float = 180.0,
                 retries: int = 2, sleep: Callable[[float], None] = time.sleep):
        if not api_key:
            raise AnthropicError("ANTHROPIC_API_KEY is not set for the scraper service (add it to .env, then restart it)")
        self._key = api_key
        self.client = client or httpx.Client(timeout=timeout)
        self.retries, self.sleep = retries, sleep
        self._thinking_pref: dict[str, int] = {}   # model -> index into THINKING_TRIES that worked

    def messages(self, *, model: str, system: str, user: str, max_tokens: int, cache_system: bool = True,
                 thinking_disabled: bool = True) -> Reply:
        body: dict = {
            "model": model, "max_tokens": max_tokens,
            "system": [{"type": "text", "text": system, **({"cache_control": {"type": "ephemeral"}} if cache_system else {})}],
            "messages": [{"role": "user", "content": user}],
        }
        if not thinking_disabled:
            return self._post(body)
        start = self._thinking_pref.get(model, 0)
        for i in range(start, len(THINKING_TRIES)):
            attempt = dict(body)
            if THINKING_TRIES[i] is not None:
                attempt["thinking"] = THINKING_TRIES[i]
            try:
                reply = self._post(attempt)
            except AnthropicError as ex:
                # this model wants a different "no thinking" setting: try the next one
                if ex.status == 400 and "thinking" in str(ex).lower() and i < len(THINKING_TRIES) - 1:
                    continue
                raise
            self._thinking_pref[model] = i
            return reply
        raise AnthropicError("no accepted thinking setting for this model", 400)   # unreachable in practice

    def _post(self, body: dict) -> Reply:
        headers = {"x-api-key": self._key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        last: Optional[AnthropicError] = None
        for attempt in range(self.retries + 1):
            try:
                r = self.client.post(API_URL, json=body, headers=headers)
            except httpx.HTTPError as ex:
                last = AnthropicError(f"could not reach the Anthropic API ({type(ex).__name__})", retryable=True)
            else:
                if r.status_code == 200:
                    try:
                        data = r.json()
                        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
                        return Reply(text=text, usage=data.get("usage") or {}, stop_reason=data.get("stop_reason"))
                    except (ValueError, AttributeError, TypeError, RecursionError):
                        # a 200 whose body is not the reply shape (a proxy page, a cut-off body): a named failure, not a crash
                        last = AnthropicError("Anthropic API returned a reply that could not be read", 200, retryable=True)
                        if attempt == self.retries:
                            break
                        self.sleep(2.0 * (attempt + 1))
                        continue
                detail = ""
                try:
                    detail = (r.json().get("error") or {}).get("message", "")
                except ValueError:
                    pass
                last = AnthropicError(f"Anthropic API returned HTTP {r.status_code}: {detail[:300]}", r.status_code,
                                      retryable=r.status_code in RETRY_STATUSES)
            if not last.retryable or attempt == self.retries:
                break
            self.sleep(2.0 * (attempt + 1))
        raise last  # type: ignore[misc]
