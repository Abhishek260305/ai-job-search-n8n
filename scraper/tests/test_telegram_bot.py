import httpx
import pytest

import telegram_bot as TB

CHAT = 5550001
BASICS = {"name": "Jane Doe", "email": "jane@example.com"}
SETTINGS = {"mode": "auto", "approval": "review", "min_score": None, "max_score": None, "daily_limit": None,
            "cover_letter": True, "max_pages": 1, "model": "claude-sonnet-5-5"}
BUDGET = {"month_spent": 1.2, "day_spent": 0.1, "monthly_cap": 10, "daily_cap": 1.5, "warn": False, "blocked": False,
          "blocked_reason": None}


class FakeStore:
    def __init__(self):
        self.offset, self.settings = 0, dict(SETTINGS)
        self.actions, self.queue = [], [{"job_id": 24, "score": 82, "title": "Backend Engineer", "company": "Acme", "needs_attention": False},
                                        {"job_id": 31, "score": 74, "title": "Platform Engineer", "company": "Globex", "needs_attention": True}]
        self.jobs = {24: {"id": 24, "title": "Backend Engineer", "company": "Acme", "status": "shortlisted", "score": 82, "verdict": "strong", "canonical_url": "https://acme.com/1"},
                     25: {"id": 25, "title": "Scam", "company": "X", "status": "scam_blocked", "score": 90},
                     26: {"id": 26, "title": "Unscored", "company": "Y", "status": "new", "score": None}}
        self.apps = {24: {"files_dir": None}}

    def telegram_offset(self): return self.offset
    def set_telegram_offset(self, n): self.offset = n
    def status_counts(self): return {"new": 5, "shortlisted": 2}
    def budget_status(self): return BUDGET
    def review_queue(self): return self.queue
    def tailor_settings(self): return self.settings

    def update_tailor_settings(self, **kw):
        if kw.get("min_score") == 99 and self.settings.get("max_score") == 50:
            raise RuntimeError("violates check constraint tailor_settings_check")
        self.settings.update(kw)
        return self.settings

    def review_application(self, job_id, action, note=None):
        if job_id == 99:
            raise RuntimeError("no application for job 99\nCONTEXT: PL/pgSQL")
        if job_id == 98:
            raise RuntimeError("cannot skip an application that is applied")
        self.actions.append((job_id, action, note))
        return {"approve": "approved", "skip": "skipped", "applied": "applied"}[action]

    def get_job(self, job_id): return self.jobs.get(job_id)
    def application(self, job_id): return self.apps.get(job_id)

    def set_application_status(self, job_id, status, note=None):
        if job_id == 99:
            raise RuntimeError("no job 99\nCONTEXT: PL/pgSQL")
        self.actions.append((job_id, "mark:" + status, note))
        return status


class FakeTG:
    def __init__(self, updates=()):
        self.updates, self.sent, self.docs, self.requested_offsets = list(updates), [], [], []

    def get_updates(self, offset):
        self.requested_offsets.append(offset)
        return [u for u in self.updates if u["update_id"] >= offset]

    def send_message(self, chat_id, text): self.sent.append((chat_id, text))
    def send_document(self, chat_id, filename, data, caption=""): self.docs.append((chat_id, filename, data))


def update(uid, text, chat=CHAT):
    return {"update_id": uid, "message": {"chat": {"id": chat}, "text": text}}


def bot(updates=(), run_tailor=None, store=None):
    store = store or FakeStore()
    tg = FakeTG(updates)
    return TB.BotHandler(store, tg, CHAT, BASICS, run_tailor or (lambda job: {"status": "drafted"})), store, tg


# ------------------------------------------------------------------ polling and the chat guard

def test_commands_from_the_authorised_chat_are_answered_and_the_offset_advances():
    b, store, tg = bot([update(10, "/help"), update(11, "/review")])
    out = b.poll()
    assert out == {"updates": 2, "handled": 2, "ignored": 0, "failed": 0} and store.offset == 12
    assert [m[0] for m in tg.sent] == [CHAT, CHAT] and "Job-search bot commands" in tg.sent[0][1] and "#24 82" in tg.sent[1][1]


def test_other_chats_are_ignored_completely_but_the_offset_still_moves():
    b, store, tg = bot([update(10, "/approve 24", chat=777), update(11, "hello", chat=777)])
    out = b.poll()
    assert out == {"updates": 2, "handled": 0, "ignored": 2, "failed": 0} and tg.sent == [] and store.actions == []
    assert store.offset == 12


def test_plain_text_from_the_owner_is_not_a_command():
    b, store, tg = bot([update(1, "thanks!")])
    assert b.poll() == {"updates": 1, "handled": 0, "ignored": 0, "failed": 0} and tg.sent == []


def test_polling_resumes_from_the_stored_offset():
    store = FakeStore(); store.offset = 12
    b, _, tg = bot([update(10, "/help"), update(12, "/status")], store=store)
    b.poll()
    assert tg.requested_offsets == [12] and len(tg.sent) == 1 and store.offset == 13


def test_a_crashing_command_is_reported_and_does_not_wedge_the_queue():
    def boom(job): raise RuntimeError("renderer down")
    b, store, tg = bot([update(1, "/tailor 24"), update(2, "/help")], run_tailor=boom)
    out = b.poll()
    assert out["failed"] == 1 and out["handled"] == 2 and store.offset == 3, "the failed update is passed over"
    assert "That command failed: RuntimeError: renderer down" in tg.sent[0][1]
    assert "Job-search bot commands" in tg.sent[1][1], "the next command still runs"
    assert b.poll() == {"updates": 0, "handled": 0, "ignored": 0, "failed": 0}, "nothing is redelivered"


def test_even_a_failure_to_report_the_failure_does_not_wedge_the_queue():
    class DeadTG(FakeTG):
        def send_message(self, chat_id, text): raise TB.BotError("Telegram sendMessage failed (ConnectError)")
    store = FakeStore()
    b = TB.BotHandler(store, DeadTG([update(1, "/help")]), CHAT, BASICS, lambda job: {})
    assert b.poll()["failed"] == 1 and store.offset == 2


# ------------------------------------------------------------------ review actions

@pytest.mark.parametrize("text,expected_action", [("/approve 24", (24, "approve", None)), ("/skip 24 not interested", (24, "skip", "not interested")),
                                                 ("/applied 24", (24, "applied", None)), ("/approve@MyBot 24", (24, "approve", None))])
def test_review_commands(text, expected_action):
    b, store, tg = bot()
    reply = b.handle(text)[0].text
    assert store.actions == [expected_action] and "Job 24" in reply


def test_approve_reply_wording():
    b, *_ = bot()
    assert "approved" in b.handle("/approve 24")[0].text and "ready to apply" in b.handle("/approve 24")[0].text
    assert "skipped" in b.handle("/skip 24")[0].text and "applied" in b.handle("/applied 24")[0].text


@pytest.mark.parametrize("text", ["/approve", "/approve abc", "/approve 24x", "/skip -1", "/approve 12345678901"])
def test_bad_job_numbers_get_a_helpful_message_and_change_nothing(text):
    b, store, _ = bot()
    assert "give a job number" in b.handle(text)[0].text and store.actions == []


def test_database_refusals_are_explained_not_dumped():
    b, *_ = bot()
    assert b.handle("/approve 99")[0].text == "⚠ no application for job 99"
    assert "cannot skip an application that is applied" in b.handle("/skip 98")[0].text
    assert "CONTEXT" not in b.handle("/approve 99")[0].text


def test_review_queue_and_empty_queue():
    b, store, _ = bot()
    text = b.handle("/review")[0].text
    assert "2 waiting" in text and "#31 74 Platform Engineer @ Globex  ⚠ needs attention" in text
    store.queue = []
    assert b.handle("/review")[0].text == "Nothing is waiting for your review."


# ------------------------------------------------------------------ status, budget, settings

def test_status_and_budget():
    b, *_ = bot()
    s = b.handle("/status")[0].text
    assert "new 5, shortlisted 2" in s and "Waiting for your review: 2" in s and "$1.20 of $10.00" in s
    assert "Today: $0.10 of $1.50" in b.handle("/budget")[0].text


def test_settings_show_and_change():
    b, store, _ = bot()
    assert "mode: auto" in b.handle("/settings")[0].text and "your search threshold" in b.handle("/settings")[0].text
    out = b.handle("/set max_score 80")[0].text
    assert store.settings["max_score"] == 80 and "max_score: 80" in out and "updated" in out
    b.handle("/set approval auto"); b.handle("/set cover_letter off"); b.handle("/set daily_limit 5"); b.handle("/set mode manual")
    assert (store.settings["approval"], store.settings["cover_letter"], store.settings["daily_limit"], store.settings["mode"]) == ("auto", False, 5, "manual")
    b.handle("/set max_score none")
    assert store.settings["max_score"] is None


@pytest.mark.parametrize("args,expect", [
    ("mode sometimes", "mode must be auto or manual"), ("approval maybe", "approval must be review, cli or auto"),
    ("min_score 500", "whole number"), ("daily_limit -3", "whole number"), ("cover_letter perhaps", "on or off"),
    ("max_pages 5", "1 or 2"), ("colour blue", "unknown setting"), ("mode", "usage"), ("", "usage"),
])
def test_bad_settings_are_rejected_with_a_reason(args, expect):
    b, store, _ = bot()
    before = dict(store.settings)
    assert expect in b.handle(f"/set {args}".strip())[0].text and store.settings == before


def test_a_setting_the_database_rejects_is_reported():
    b, store, _ = bot()
    store.settings["max_score"] = 50
    assert "rejected" in b.handle("/set min_score 99")[0].text


@pytest.mark.parametrize("key,val,expected", [("min_score", "none", None), ("max_score", "101", 101), ("max_score", "102", "err"), ("mode", "AUTO", "auto")])
def test_parse_setting(key, val, expected):
    if expected == "err":
        with pytest.raises(ValueError):
            TB.parse_setting(key, val)
    else:
        assert TB.parse_setting(key, val)[1] == expected


# ------------------------------------------------------------------ /tailor and files

def test_tailor_runs_the_service_and_replies_with_the_review_message_and_pdfs(tmp_path):
    (tmp_path / "resume.pdf").write_bytes(b"%PDF-resume"); (tmp_path / "cover_letter.pdf").write_bytes(b"%PDF-cover")
    result = {"status": "drafted", "variant": "tailored", "files_dir": str(tmp_path), "has_cover_letter": True,
              "requirements": {"matched": 3, "bridged": 1, "gap": 0}, "attention": [], "audit": {"soft": []}, "cost_usd": 0.04}
    seen = {}
    b, *_ = bot(run_tailor=lambda job: seen.update(job=job) or result)
    reply = b.handle("/tailor 24")[0]
    assert seen["job"]["id"] == 24 and "Backend Engineer @ Acme" in reply.text and "/approve 24" in reply.text
    assert [n for n, _ in reply.documents] == ["jane-doe_resume_acme.pdf", "jane-doe_cover_letter_acme.pdf"]
    assert reply.documents[0][1] == b"%PDF-resume"


@pytest.mark.parametrize("job_id,expect", [(25, "scam_blocked"), (26, "not been scored"), (77, "no job 77")])
def test_tailor_refuses_jobs_it_should_not_touch(job_id, expect):
    called = []
    b, *_ = bot(run_tailor=lambda job: called.append(job) or {})
    assert expect in b.handle(f"/tailor {job_id}")[0].text and called == []


def test_tailor_failures_are_reported_plainly():
    b, *_ = bot(run_tailor=lambda job: {"status": "budget_paused", "reason": "not enough budget left for this job"})
    assert "Could not tailor job 24: not enough budget" in b.handle("/tailor 24")[0].text


def test_files_resends_the_pdfs(tmp_path):
    (tmp_path / "resume.pdf").write_bytes(b"%PDF-r")
    b, store, _ = bot()
    store.apps[24] = {"files_dir": str(tmp_path)}
    r = b.handle("/files 24")[0]
    assert [n for n, _ in r.documents] == ["jane-doe_resume_acme.pdf"]
    assert "no application yet" in b.handle("/files 26")[0].text


def test_notify_sends_the_message_and_the_documents(tmp_path):
    (tmp_path / "resume.pdf").write_bytes(b"%PDF-r")
    b, store, tg = bot()
    b.notify({"status": "drafted", "variant": "base", "files_dir": str(tmp_path), "has_cover_letter": False, "requirements": {},
              "attention": ["the resume is 2 pages (limit 1)"], "audit": {"soft": []}, "cost_usd": 0}, store.jobs[24])
    assert "Application ready for review" in tg.sent[0][1] and "the resume is 2 pages" in tg.sent[0][1]
    assert tg.docs and tg.docs[0][1] == "jane-doe_resume_acme.pdf"


def test_unknown_commands_get_a_nudge():
    b, *_ = bot()
    assert "/help" in b.handle("/frobnicate")[0].text


# ------------------------------------------------------------------ the HTTP client

def client(handler):
    return TB.TelegramClient("123456:SECRET-token", httpx.Client(transport=httpx.MockTransport(handler)))


def test_client_calls_and_chunking():
    seen = []

    def handler(req):
        seen.append((str(req.url), req.content))
        return httpx.Response(200, json={"ok": True, "result": []})
    c = client(handler)
    c.get_updates(5)
    c.send_message(1, "x" * 8000)
    c.send_document(1, "a.pdf", b"%PDF", "cap")
    assert seen[0][0].endswith("/getUpdates") and b'"offset":5' in seen[0][1]
    assert sum(1 for u, _ in seen if u.endswith("/sendMessage")) == 3, "long messages are split"
    assert seen[-1][0].endswith("/sendDocument") and b"a.pdf" in seen[-1][1]


def test_client_errors_never_contain_the_token():
    def rejected(req):
        return httpx.Response(200, json={"ok": False, "description": "Bad Request: chat not found"})

    def down(req):
        raise httpx.ConnectError("could not connect to /bot123456:SECRET-token/")
    with pytest.raises(TB.BotError, match="chat not found"):
        client(rejected).send_message(1, "hi")
    with pytest.raises(TB.BotError) as e:
        client(down).get_updates(0)
    assert "SECRET" not in str(e.value) and "ConnectError" in str(e.value)


def test_a_missing_token_is_a_clear_error():
    with pytest.raises(TB.BotError, match="TELEGRAM_BOT_TOKEN"):
        TB.TelegramClient("")


# ------------------------------------------------------------------ settings from the API/CLI

def test_normalise_settings_accepts_the_same_values_as_set():
    assert TB.normalise_settings({"mode": "manual", "min_score": 65, "max_score": None, "daily_limit": 5,
                                  "cover_letter": False, "max_pages": 2, "approval": "auto"}) == {
        "mode": "manual", "min_score": 65, "max_score": None, "daily_limit": 5, "cover_letter": False, "max_pages": 2, "approval": "auto"}
    assert TB.normalise_settings({"model": "claude-haiku-5-5"}) == {"model": "claude-haiku-5-5"}


@pytest.mark.parametrize("fields", [{"mode": "sometimes"}, {"min_score": 500}, {"max_pages": 3}, {"model": "bad model!"},
                                     {"model": 5}, {"colour": "blue"}, {"daily_limit": -1}])
def test_normalise_settings_rejects_bad_values(fields):
    with pytest.raises(ValueError):
        TB.normalise_settings(fields)


def test_the_offset_moves_before_a_command_runs_so_a_slow_command_cannot_run_twice():
    seen = []
    store = FakeStore()

    def slow_tailor(job):
        seen.append(store.offset)       # what a concurrent poll would read while this command is still running
        return {"status": "drafted", "variant": "tailored", "files_dir": None, "has_cover_letter": False,
                "requirements": {}, "attention": [], "audit": {"soft": []}, "cost_usd": 0}
    b = TB.BotHandler(store, FakeTG([update(10, "/tailor 24")]), CHAT, BASICS, slow_tailor)
    b.poll()
    assert seen == [11], "the update was already acknowledged while the command ran"
    assert FakeTG([update(10, "/tailor 24")]).get_updates(store.offset) == [], "a second poll gets nothing to redo"


# ------------------------------------------------------------------ manual stages and the cli-only approval

@pytest.mark.parametrize("text,expected", [
    ("/mark 24 interview", (24, "mark:interview", None)),
    ("/mark 24 Interview phone screen Tuesday", (24, "mark:interview", "phone screen Tuesday")),
    ("/mark 24 in-progress", (24, "mark:in_progress", None)),
    ("/mark 24 rejected no thanks", (24, "mark:rejected", "no thanks"))])
def test_mark_sets_any_stage_by_hand(text, expected):
    b, store, _ = bot()
    reply = b.handle(text)[0].text
    assert store.actions == [expected] and reply == f"Job 24: marked {expected[1].split(':')[1]}."


@pytest.mark.parametrize("text", ["/mark", "/mark 24", "/mark 24 hired", "/mark abc interview"])
def test_mark_asks_for_what_it_needs_and_changes_nothing(text):
    b, store, _ = bot()
    reply = b.handle(text)[0].text
    assert reply.startswith("⚠") and store.actions == []


def test_mark_explains_database_refusals():
    b, *_ = bot()
    assert b.handle("/mark 99 interview")[0].text == "⚠ no job 99"


def test_help_lists_mark_and_the_cli_approval():
    b, *_ = bot()
    text = b.handle("/help")[0].text
    assert "/mark 24 interview" in text and "review|cli|auto" in text


def test_the_cli_approval_can_be_set_from_telegram():
    b, store, _ = bot()
    b.handle("/set approval cli")
    assert store.settings["approval"] == "cli"
    assert "cli asks only at the command line" in b.handle("/settings")[0].text
    assert "approval must be review, cli or auto" in b.handle("/set approval whenever")[0].text


# ------------------------------------------------------------------ paper size and style notes

def test_paper_and_style_notes_settings():
    assert TB.parse_setting("paper", "Letter") == ("paper", "letter") and TB.parse_setting("paper", "a4") == ("paper", "a4")
    with pytest.raises(ValueError):
        TB.parse_setting("paper", "a5")
    assert TB.parse_setting("style_notes", "  Warm   but formal ") == ("style_notes", "Warm but formal")
    for clear in ("none", "off", "-", "NONE"):
        assert TB.parse_setting("style_notes", clear) == ("style_notes", None)
    with pytest.raises(ValueError, match="400"):
        TB.parse_setting("style_notes", "x" * 401)


def test_style_notes_can_be_a_whole_sentence_in_telegram():
    b, store, _ = bot()
    reply = b.handle("/set style_notes Keep the tone warm but formal. No exclamation marks.")[0].text
    assert store.settings["style_notes"] == "Keep the tone warm but formal. No exclamation marks." and "style_notes: Keep the tone warm" in reply
    b.handle("/set style_notes none")
    assert store.settings["style_notes"] is None
    assert "usage" in b.handle("/set paper")[0].text and b.handle("/set paper letter")[0].text.startswith("✅")
    assert store.settings["paper"] == "letter"


def test_normalise_settings_handles_the_new_options():
    assert TB.normalise_settings({"paper": "letter", "style_notes": None}) == {"paper": "letter", "style_notes": None}
    assert TB.normalise_settings({"style_notes": "Plain and direct"}) == {"style_notes": "Plain and direct"}
