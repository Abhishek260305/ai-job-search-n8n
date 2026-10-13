"""Optional parts: what is on, why a part is off, and how approval behaves without Telegram."""
import pytest

import features as F

TG = {"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "424242"}
ALL_ON = {"tailoring": True, "telegram": True, "digest": True}
GMAIL = {"GMAIL_CLIENT_ID": "id.apps.example", "GMAIL_CLIENT_SECRET": "not-a-real-secret", "GMAIL_REFRESH_TOKEN": "not-a-real-token"}


@pytest.mark.parametrize("env,expected", [
    (TG, True),
    ({"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "-100123"}, True),       # group chat ids are negative
    ({}, False),
    ({"TELEGRAM_BOT_TOKEN": "123:abc"}, False),
    ({"TELEGRAM_CHAT_ID": "424242"}, False),
    ({"TELEGRAM_BOT_TOKEN": "  ", "TELEGRAM_CHAT_ID": "424242"}, False),
    ({"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "not-a-number"}, False),
    ({"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": ""}, False)])
def test_telegram_configured(env, expected):
    assert F.telegram_configured(env) is expected


def test_everything_on_when_configured():
    eff = F.effective({**ALL_ON, "gmail": True}, {**TG, **GMAIL})
    assert all(e["on"] and e["reason"] == "" for e in eff.values()) and set(eff) == {"tailoring", "telegram", "digest", "gmail"}


def test_telegram_is_optional_and_says_how_to_add_it():
    eff = F.effective(ALL_ON, {})
    assert eff["telegram"]["on"] is False and "TELEGRAM_BOT_TOKEN" in eff["telegram"]["reason"] and "optional" in eff["telegram"]["reason"]
    assert eff["digest"] == {"on": False, "reason": "needs Telegram"}, "the digest cannot exist without Telegram"
    assert eff["tailoring"]["on"] is True, "tailoring does not depend on Telegram"


def test_a_configured_telegram_can_still_be_switched_off():
    eff = F.effective(dict(ALL_ON, telegram=False), TG)
    assert eff["telegram"]["on"] is False and "switched off" in eff["telegram"]["reason"] and eff["digest"]["on"] is False


def test_digest_can_be_off_while_telegram_stays_on():
    eff = F.effective(dict(ALL_ON, digest=False), TG)
    assert eff["telegram"]["on"] is True and eff["digest"]["on"] is False and "switched off" in eff["digest"]["reason"]


def test_tailoring_off_is_the_stop_at_scoring_path():
    eff = F.effective(dict(ALL_ON, tailoring=False), TG)
    assert eff["tailoring"]["on"] is False and "features set --tailoring on" in eff["tailoring"]["reason"]
    assert eff["telegram"]["on"] is True, "you may still want the digest and /status without tailoring"


def test_missing_flags_default_to_on_except_gmail_which_defaults_to_off():
    eff = F.effective({}, {**TG, **GMAIL})
    assert {n for n, e in eff.items() if e["on"]} == {"tailoring", "telegram", "digest"}, "a part that needs a Google login is only used when asked for"
    assert eff["gmail"] == {"on": False, "reason": "switched off (jobsearch features set --gmail on)"}


@pytest.mark.parametrize("env,expected", [
    (GMAIL, True),
    ({}, False),
    ({"GMAIL_CLIENT_ID": "x"}, False),
    ({"GMAIL_CLIENT_ID": "x", "GMAIL_CLIENT_SECRET": "y"}, False),
    ({"GMAIL_CLIENT_SECRET": "y", "GMAIL_REFRESH_TOKEN": "z"}, False),
    ({**GMAIL, "GMAIL_REFRESH_TOKEN": "   "}, False),
    ({**GMAIL, "GMAIL_CLIENT_ID": ""}, False)])
def test_gmail_configured_needs_all_three_values(env, expected):
    assert F.gmail_configured(env) is expected


def test_gmail_is_off_until_asked_for_then_needs_the_login_and_says_how_to_add_it():
    off = F.effective(ALL_ON, GMAIL)["gmail"]
    assert off["on"] is False and "features set --gmail on" in off["reason"]
    unconfigured = F.effective({**ALL_ON, "gmail": True}, TG)["gmail"]
    assert unconfigured["on"] is False and "not configured" in unconfigured["reason"] and "docs/gmail.md" in unconfigured["reason"]
    for name in F.GMAIL_VARS:
        assert name in unconfigured["reason"], "it names every value that is missing"
    assert F.effective({**ALL_ON, "gmail": True}, GMAIL)["gmail"] == {"on": True, "reason": ""}


def test_gmail_does_not_depend_on_telegram_or_tailoring():
    eff = F.effective({"tailoring": False, "telegram": False, "digest": False, "gmail": True}, GMAIL)
    assert eff["gmail"]["on"] is True, "suggestions are shown in the dashboard, so Telegram is not needed"
    eff = F.effective({**ALL_ON, "gmail": False}, {**TG, **GMAIL})
    assert eff["tailoring"]["on"] and eff["telegram"]["on"] and eff["digest"]["on"], "switching Gmail off touches nothing else"


def test_a_secret_never_appears_in_the_reason():
    for flags in ({"gmail": False}, {"gmail": True}):
        for env in (GMAIL, {"GMAIL_CLIENT_ID": "id.apps.example"}):
            assert "not-a-real" not in F.effective({**ALL_ON, **flags}, env)["gmail"]["reason"]
            assert "id.apps.example" not in F.effective({**ALL_ON, **flags}, env)["gmail"]["reason"]


@pytest.mark.parametrize("approval,telegram,expected", [
    ("review", True, "review"),
    ("review", False, "auto"),      # nobody to ask: store ready, as the user asked for
    ("cli", True, "cli"),
    ("cli", False, "cli"),          # a human decision at the command line needs no Telegram
    ("auto", True, "auto"),
    ("auto", False, "auto")])
def test_effective_approval(approval, telegram, expected):
    assert F.effective_approval(approval, telegram) == expected


def test_effective_approval_rejects_unknown_values():
    with pytest.raises(ValueError):
        F.effective_approval("whenever", True)


def test_only_cli_approval_means_never_push():
    assert F.should_notify("review") and F.should_notify("auto") and not F.should_notify("cli")


def test_describe_explains_the_fallback():
    text = "\n".join(F.describe(ALL_ON, {}, "review"))
    assert "scoring:    always on" in text and "telegram:   off, not configured" in text
    assert "review → behaves as auto" in text
    assert "approval:   review" in "\n".join(F.describe(ALL_ON, TG, "review")) and "behaves as" not in "\n".join(F.describe(ALL_ON, TG, "review"))


def test_describe_lists_gmail_as_off_by_default():
    text = "\n".join(F.describe(ALL_ON, TG, "review"))
    assert "gmail:      off, switched off (jobsearch features set --gmail on)" in text
