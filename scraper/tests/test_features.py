"""Optional parts: what is on, why a part is off, and how approval behaves without Telegram."""
import pytest

import features as F

TG = {"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "424242"}
ALL_ON = {"tailoring": True, "telegram": True, "digest": True}


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
    eff = F.effective(ALL_ON, TG)
    assert all(e["on"] and e["reason"] == "" for e in eff.values()) and set(eff) == {"tailoring", "telegram", "digest"}


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
    assert eff["tailoring"]["on"] is False and "features set tailoring on" in eff["tailoring"]["reason"]
    assert eff["telegram"]["on"] is True, "you may still want the digest and /status without tailoring"


def test_missing_flags_default_to_on():
    assert all(e["on"] for e in F.effective({}, TG).values())


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
