"""Budget guard: how many paid model calls may run, given the caps. Pure stdlib.

Two ideas make this a hard stop rather than a report:

  1. Worst-case reservation. Before a batch is claimed, each job is priced at its WORST case
     (a long prompt, a cache write, and the full max_tokens of output). A batch only claims as
     many jobs as the remaining monthly AND daily budget can pay for even in that worst case.
  2. Unknown prices fail expensive. A model missing from the price table is priced at the most
     expensive known rates (done in SQL: usage_cost / model_pricing), so a typo can only make
     the guard stricter.

Actual usage, as reported by the provider, is logged after every call (llm_usage), so the month's
spend is real numbers, and the caps are checked against that log.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

# Prompt text is priced at 1 token per 3 characters: more tokens than the usual ~4 chars/token,
# so the estimate errs on the expensive side.
WORST_CASE_CHARS_PER_TOKEN = 3


def worst_case_item_cost(pricing: dict, prompt_chars: int, max_tokens: int) -> float:
    """Highest plausible cost of one call, in dollars. `pricing` holds per-million-token rates."""
    in_rate = max(float(pricing["input_per_mtok"]), float(pricing["cache_write_per_mtok"]))
    est_input_tokens = prompt_chars / WORST_CASE_CHARS_PER_TOKEN
    return (est_input_tokens * in_rate + max_tokens * float(pricing["output_per_mtok"])) / 1_000_000


def allowed_items(status: dict, limit: int, per_item_cost: float) -> int:
    """How many items a batch may claim: bounded by the request, the monthly remainder and the
    daily remainder, each priced at the worst case. Zero when a cap is already reached."""
    if status.get("blocked") or per_item_cost <= 0:
        return 0 if status.get("blocked") else max(limit, 0)
    # exact decimal arithmetic: money should not be floored through binary floating point
    remaining = min(Decimal(str(status["month_remaining"])), Decimal(str(status["day_remaining"])))
    affordable = int(remaining // Decimal(str(per_item_cost)))
    return max(0, min(limit, affordable))


def normalise_usage(usage: Optional[dict]) -> dict:
    """The token counts of a provider response, defaulting to zero. Anthropic reports
    input_tokens (uncached), output_tokens, cache_creation_input_tokens and cache_read_input_tokens."""
    u = usage or {}

    def n(key: str) -> int:
        v = u.get(key)
        return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else 0

    return {"input_tokens": n("input_tokens"), "output_tokens": n("output_tokens"),
            "cache_write_tokens": n("cache_creation_input_tokens"), "cache_read_tokens": n("cache_read_input_tokens")}


def summarise(status: dict, per_item_cost: Optional[float] = None, allowed: Optional[int] = None) -> dict:
    out = {"month_spent": round(float(status["month_spent"]), 4), "monthly_cap": float(status["monthly_cap"]),
           "day_spent": round(float(status["day_spent"]), 4), "daily_cap": float(status["daily_cap"]),
           "blocked": bool(status["blocked"]), "reason": status.get("blocked_reason"), "warn": bool(status["warn"])}
    if per_item_cost is not None:
        out["worst_case_per_job"] = round(per_item_cost, 6)
    if allowed is not None:
        out["allowed_jobs"] = allowed
    return out


def alert_text(status: dict) -> str:
    """A one-line warning when spend is high or paused, else an empty string."""
    month, cap = float(status["month_spent"]), float(status["monthly_cap"])
    if status.get("blocked"):
        return (f"⚠ Claude spending cap reached ({status.get('blocked_reason')}): scoring is paused. "
                f"This month: ${month:.2f} of ${cap:.2f}.")
    if status.get("warn"):
        pct = round(100 * month / cap) if cap else 100
        return f"⚠ Claude spend is at {pct}% of your ${cap:.2f} monthly cap (${month:.2f} so far)."
    return ""


def footer_text(status: dict) -> str:
    return f"Claude spend this month: ${float(status['month_spent']):.2f} of ${float(status['monthly_cap']):.2f}"
