import json

import httpx
import pytest

import anthropic_client as A

KEY = "sk-ant-SECRET-test-key"


def client_for(handler, **kw):
    return A.AnthropicClient(KEY, httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None, **kw)


def ok(text="hi", **usage):
    return httpx.Response(200, json={"content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": text}],
                                      "usage": {"input_tokens": 10, "output_tokens": 5, **usage}, "stop_reason": "end_turn"})


def test_request_shape_and_reply():
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content); seen["headers"] = req.headers
        return ok("{\"a\":1}", cache_read_input_tokens=7)
    r = client_for(handler).messages(model="claude-sonnet-5-5", system="SYS", user="USER", max_tokens=123)
    b = seen["body"]
    assert b["model"] == "claude-sonnet-5-5" and b["max_tokens"] == 123 and b["thinking"] == {"type": "disabled"}
    assert b["system"] == [{"type": "text", "text": "SYS", "cache_control": {"type": "ephemeral"}}]
    assert b["messages"] == [{"role": "user", "content": "USER"}]
    assert seen["headers"]["x-api-key"] == KEY and seen["headers"]["anthropic-version"] == "2023-06-01"
    assert r.text == '{"a":1}' and r.usage["cache_read_input_tokens"] == 7 and r.stop_reason == "end_turn"


def test_options_can_be_switched_off():
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        return ok()
    client_for(handler).messages(model="m", system="S", user="U", max_tokens=1, cache_system=False, thinking_disabled=False)
    assert "thinking" not in seen["body"] and "cache_control" not in seen["body"]["system"][0]


def test_a_missing_key_is_a_clear_error():
    with pytest.raises(A.AnthropicError, match="ANTHROPIC_API_KEY"):
        A.AnthropicClient("")


def test_retryable_errors_are_retried_then_succeed():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(529, json={"error": {"message": "overloaded"}}) if len(calls) < 3 else ok("done")
    assert client_for(handler).messages(model="m", system="s", user="u", max_tokens=1).text == "done" and len(calls) == 3


def test_it_gives_up_after_the_retry_limit_without_leaking_the_key():
    def handler(req):
        return httpx.Response(529, json={"error": {"message": "overloaded"}})
    with pytest.raises(A.AnthropicError) as e:
        client_for(handler, retries=1).messages(model="m", system="s", user="u", max_tokens=1)
    assert e.value.status == 529 and e.value.retryable and KEY not in str(e.value)


def test_client_errors_are_not_retried():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(400, json={"error": {"message": "max_tokens too large"}})
    with pytest.raises(A.AnthropicError, match="max_tokens too large") as e:
        client_for(handler).messages(model="m", system="s", user="u", max_tokens=1)
    assert len(calls) == 1 and not e.value.retryable and e.value.status == 400


def test_a_non_json_error_body_is_handled():
    with pytest.raises(A.AnthropicError, match="HTTP 502"):
        client_for(lambda req: httpx.Response(502, text="<html>bad gateway</html>"), retries=0).messages(model="m", system="s", user="u", max_tokens=1)


def test_network_errors_are_retryable_and_never_leak_details():
    def handler(req):
        raise httpx.ConnectError(f"cannot connect with {KEY}")
    with pytest.raises(A.AnthropicError) as e:
        client_for(handler, retries=0).messages(model="m", system="s", user="u", max_tokens=1)
    assert e.value.retryable and "ConnectError" in str(e.value) and KEY not in str(e.value)


# ------------------------------------------------------------------ models disagree about "no thinking"

def thinking_aware(accepts):
    """A fake API that only accepts one thinking setting (None = no setting at all)."""
    seen = []

    def handler(req):
        b = json.loads(req.content)
        seen.append(b.get("thinking"))
        if b.get("thinking") == accepts:
            return ok("fine")
        return httpx.Response(400, json={"error": {"message": 'To turn thinking off on this model, send "thinking": {"type": "between_tools"} instead'}})
    return handler, seen


def test_a_model_that_wants_a_different_thinking_setting_is_handled_and_remembered():
    handler, seen = thinking_aware({"type": "between_tools"})
    c = client_for(handler)
    assert c.messages(model="claude-sonnet-5-5", system="s", user="u", max_tokens=1).text == "fine"
    assert seen == [{"type": "disabled"}, {"type": "between_tools"}]
    seen.clear()
    c.messages(model="claude-sonnet-5-5", system="s", user="u", max_tokens=1)
    assert seen == [{"type": "between_tools"}], "the working setting is remembered for that model"


def test_each_model_is_learned_separately():
    handler, seen = thinking_aware({"type": "disabled"})
    c = client_for(handler)
    c.messages(model="claude-haiku-5-5", system="s", user="u", max_tokens=1)
    assert seen == [{"type": "disabled"}]


def test_as_a_last_resort_no_thinking_setting_is_sent():
    handler, seen = thinking_aware(None)
    assert client_for(handler).messages(model="m", system="s", user="u", max_tokens=1).text == "fine"
    assert seen == [{"type": "disabled"}, {"type": "between_tools"}, None]


def test_unrelated_400_errors_are_not_mistaken_for_a_thinking_problem():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(400, json={"error": {"message": "max_tokens: must be at most 8192"}})
    with pytest.raises(A.AnthropicError, match="max_tokens"):
        client_for(handler).messages(model="m", system="s", user="u", max_tokens=1)
    assert len(calls) == 1
