from decimal import Decimal

import pytest

import budget as B

HAIKU = {"input_per_mtok": Decimal("0.10"), "output_per_mtok": Decimal("0.50"),
         "cache_write_per_mtok": Decimal("0.125"), "cache_read_per_mtok": Decimal("0.01")}
SONNET = {"input_per_mtok": Decimal("2"), "output_per_mtok": Decimal("10"),
          "cache_write_per_mtok": Decimal("2.5"), "cache_read_per_mtok": Decimal("0.1")}


def status(**over):
    s = {"month_spent": Decimal("0"), "day_spent": Decimal("0"), "monthly_cap": Decimal("10"),
         "daily_cap": Decimal("1.5"), "month_remaining": Decimal("10"), "day_remaining": Decimal("1.5"),
         "warn": False, "blocked": False, "blocked_reason": None}
    s.update(over)
    return s


# ------------------------------------------------------------------ worst-case cost

def test_worst_case_uses_the_priciest_input_rate_and_full_output():
    # 9,000 chars / 3 = 3,000 tokens at the cache-write rate (higher than plain input) + 700 output tokens
    cost = B.worst_case_item_cost(HAIKU, 9000, 700)
    assert cost == pytest.approx((3000 * 0.125 + 700 * 0.5) / 1e6)


def test_worst_case_is_never_below_a_typical_real_call():
    typical = (2500 * 0.10 + 350 * 0.50) / 1e6          # what a real scoring call costs
    assert B.worst_case_item_cost(HAIKU, 12000, 700) > typical * 1.5


def test_pricier_model_costs_proportionally_more():
    h, s = B.worst_case_item_cost(HAIKU, 9000, 700), B.worst_case_item_cost(SONNET, 9000, 700)
    assert s / h == pytest.approx(19, rel=0.15)


# ------------------------------------------------------------------ how many jobs may be claimed

def test_batch_is_limited_by_the_request_when_budget_is_plentiful():
    assert B.allowed_items(status(), 10, 0.0008) == 10


def test_batch_shrinks_to_what_the_remaining_budget_can_pay_for():
    s = status(month_remaining=Decimal("0.01"), day_remaining=Decimal("1.5"))
    assert B.allowed_items(s, 10, 0.0008) == 10          # 0.01 / 0.0008 = 12.5, capped by the request
    s = status(month_remaining=Decimal("0.0024"), day_remaining=Decimal("1.5"))
    assert B.allowed_items(s, 10, 0.0008) == 3
    s = status(month_remaining=Decimal("10"), day_remaining=Decimal("0.0016"))
    assert B.allowed_items(s, 10, 0.0008) == 2, "the daily remainder binds when it is smaller"


def test_worst_case_total_can_never_exceed_the_remaining_budget():
    for remaining in (0.0001, 0.0007, 0.0008, 0.0023, 0.5, 3.0):
        s = status(month_remaining=Decimal(str(remaining)), day_remaining=Decimal("100"))
        n = B.allowed_items(s, 1000, 0.0008)
        assert n * 0.0008 <= remaining + 1e-12


def test_nothing_is_claimed_at_a_cap():
    assert B.allowed_items(status(blocked=True, blocked_reason="monthly cap reached"), 10, 0.0008) == 0
    assert B.allowed_items(status(month_remaining=Decimal("0")), 10, 0.0008) == 0
    assert B.allowed_items(status(day_remaining=Decimal("0")), 10, 0.0008) == 0
    assert B.allowed_items(status(month_remaining=Decimal("0.0001")), 10, 0.0008) == 0   # cannot afford even one


# ------------------------------------------------------------------ usage normalisation

def test_anthropic_usage_is_mapped_to_our_columns():
    u = B.normalise_usage({"input_tokens": 1200, "output_tokens": 340, "cache_creation_input_tokens": 1300,
                           "cache_read_input_tokens": 0, "service_tier": "standard"})
    assert u == {"input_tokens": 1200, "output_tokens": 340, "cache_write_tokens": 1300, "cache_read_tokens": 0}


@pytest.mark.parametrize("bad", [None, {}, {"input_tokens": "lots"}, {"input_tokens": -5}, {"output_tokens": True}, "x"])
def test_missing_or_bad_usage_counts_as_zero_instead_of_crashing(bad):
    if isinstance(bad, str):
        bad = None
    assert B.normalise_usage(bad) == {"input_tokens": 0, "output_tokens": 0, "cache_write_tokens": 0, "cache_read_tokens": 0}


# ------------------------------------------------------------------ messages

def test_alert_levels():
    assert B.alert_text(status()) == ""
    warn = B.alert_text(status(warn=True, month_spent=Decimal("8.5")))
    assert "85%" in warn and "$8.50" in warn and "paused" not in warn
    paused = B.alert_text(status(blocked=True, blocked_reason="daily cap reached", month_spent=Decimal("1.6")))
    assert "daily cap reached" in paused and "paused" in paused


def test_footer_and_summary():
    assert B.footer_text(status(month_spent=Decimal("1.234"))) == "Claude spend this month: $1.23 of $10.00"
    out = B.summarise(status(month_spent=Decimal("1.23456")), per_item_cost=0.000812345, allowed=7)
    assert out["month_spent"] == 1.2346 and out["worst_case_per_job"] == 0.000812 and out["allowed_jobs"] == 7
    assert out["blocked"] is False and out["monthly_cap"] == 10.0
