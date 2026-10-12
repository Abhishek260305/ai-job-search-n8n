import json
import os
from datetime import date, datetime, timedelta, timezone

import pytest

import pipeline as PL
import scoring

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
SETTINGS = {"search_terms": ["backend engineer"], "locations": ["Pune, India", "London, United Kingdom"],
            "sources": ["indeed", "naukri"], "allow_remote": True, "min_yoe": 1, "max_yoe": 4,
            "include_keywords": [], "exclude_keywords": ["lead", "intern"], "exclude_companies": [],
            "max_age_days": 7, "score_threshold": 70}


class FakeStore:
    """In-memory stand-in for db.PgStore, mimicking the SQL functions' contract."""

    def __init__(self, settings=SETTINGS):
        self.settings = settings
        self.jobs = {}        # id -> dict
        self.by_key = {}      # (source, source_job_id) -> id
        self.cursor = 0
        self.failures = {}
        self.notified = []
        self.recorded = []
        self.usage = []
        self.budget = {"month_spent": 0, "day_spent": 0, "monthly_cap": 10, "daily_cap": 1.5, "month_remaining": 10,
                       "day_remaining": 1.5, "warn": False, "blocked": False, "blocked_reason": None}
        self.claim_limits = []

    def budget_status(self):
        return self.budget

    def pricing(self, model):
        return {"input_per_mtok": 0.10, "output_per_mtok": 0.50, "cache_write_per_mtok": 0.125, "cache_read_per_mtok": 0.01}

    def record_usage(self, provider, purpose, model, job_id, usage, ok=True, note=None, cost_override=None):
        self.usage.append({"provider": provider, "purpose": purpose, "model": model, "job_id": job_id,
                           "usage": usage, "ok": ok, "note": note})
        return 0.0

    def search_settings(self, profile="default"):
        return self.settings

    def next_slice(self, profile, total, batch):
        start = self.cursor % total
        self.cursor = (start + batch) % total
        return start

    def upsert(self, j, assessment):
        key = (j["source"], str(j["source_job_id"]))
        if key in self.by_key:
            return self.by_key[key], False
        jid = len(self.jobs) + 1
        self.by_key[key] = jid
        self.jobs[jid] = {"id": jid, "title": j["title"], "company": j["company"], "location": j.get("location"),
                          "description": j.get("description"), "posted_at": None, "yoe": (None, None),
                          "status": "scam_blocked" if assessment["risk"] == "high" else "new",
                          "scam_risk": assessment["risk"], "filter_reason": None}
        return jid, True

    def status_of(self, jid):
        return self.jobs[jid]["status"]

    def set_yoe(self, jid, lo, hi):
        self.jobs[jid]["yoe"] = (lo, hi)

    def mark_filtered(self, jid, reason, lo, hi):
        self.jobs[jid].update(status="filtered_out", filter_reason=reason, yoe=(lo, hi))

    def claim(self, limit):
        self.claim_limits.append(limit)
        rows = [dict(j) for j in self.jobs.values() if j["status"] == "new" and j["scam_risk"] != "high"][:limit]
        for r in rows:
            self.jobs[r["id"]]["claimed"] = True
        return rows

    def note_failure(self, jid):
        self.failures[jid] = self.failures.get(jid, 0) + 1
        return self.failures[jid]

    def record_score(self, jid, result):
        self.recorded.append((jid, result))
        self.jobs[jid]["status"] = result["next_status"]
        return result["next_status"]

    def digest_rows(self, limit):
        return []

    def blocked_rows(self):
        return [{"id": j["id"], "title": j["title"], "company": j["company"], "location": j["location"],
                 "description": j["description"], "url": None, "posted_at": None, "is_remote": False}
                for j in self.jobs.values() if j["status"] == "scam_blocked"]

    def set_safety(self, jid, assessment, unblock):
        self.jobs[jid]["scam_risk"] = assessment["risk"]
        if unblock and self.jobs[jid]["status"] == "scam_blocked":
            self.jobs[jid].update(status="new", filter_reason=None)

    def sweep(self):
        return 3


def raw(i, **over):
    j = {"source": "indeed", "source_job_id": f"id{i}", "url": f"https://www.indeed.com/viewjob?jk={i}",
         "title": "Backend Engineer", "company": f"Company {i}", "location": "Pune, India", "is_remote": False,
         "posted_at": "2026-10-09T00:00:00", "description": "Build APIs in Go. 2-4 years of experience.",
         "apply_url": None}
    j.update(over)
    return j


# ------------------------------------------------------------------ plan

def test_plan_rotates_through_the_combinations():
    store = FakeStore()
    first = PL.plan(store, batch=2)
    second = PL.plan(store, batch=2)
    # Pune x (indeed, naukri) + London x indeed (naukri is skipped outside India) = 3 combinations
    assert first["total"] == 3 and first["start"] == 0 and second["start"] == 2
    assert len(first["requests"]) == 2 and first["requests"][0]["endpoint"] == "/search"
    labels = [r["label"] for r in first["requests"] + second["requests"]]
    assert len(set(labels)) == 3 and len(labels) == 4  # the second slice wraps back to the first item


def test_plan_with_no_usable_combinations_says_so():
    out = PL.plan(FakeStore(dict(SETTINGS, search_terms=[])))
    assert out["requests"] == [] and "no usable" in out["note"]


# ------------------------------------------------------------------ ingest

def test_ingest_keeps_good_jobs_and_filters_the_rest_with_reasons():
    store = FakeStore()
    jobs = [raw(1), raw(2, title="Tech Lead"), raw(3, description="8+ years of experience required"),
            raw(4, posted_at="2026-09-01T00:00:00")]
    out = PL.ingest(store, jobs, SETTINGS, NOW)
    assert (out["received"], out["new"], out["kept"], out["filtered_out"]) == (4, 4, 1, 3)
    reasons = {j["filter_reason"] for j in store.jobs.values() if j["filter_reason"]}
    assert any("excluded word" in r for r in reasons) and any("asks for 8+" in r for r in reasons)
    assert out["top_filter_reasons"]
    assert store.jobs[1]["status"] == "new" and store.jobs[1]["yoe"] == (2, 4)


def test_ingest_blocks_scams_and_does_not_trust_the_callers_safety_field():
    store = FakeStore()
    scam = raw(1, description="Pay a refundable registration fee of Rs 500 to confirm.",
               safety={"risk": "low", "score": 0, "flags": []})  # a lying caller
    out = PL.ingest(store, [scam, raw(2)], SETTINGS, NOW)
    assert out["scam_blocked"] == 1 and out["kept"] == 1
    assert store.jobs[1]["status"] == "scam_blocked"


def test_ingest_counts_duplicates_and_invalid_rows():
    store = FakeStore()
    PL.ingest(store, [raw(1)], SETTINGS, NOW)
    out = PL.ingest(store, [raw(1), raw(2, title=""), raw(3, company=None), {"title": "x"}, raw(4)], SETTINGS, NOW)
    assert out["seen"] == 1 and out["new"] == 1 and out["invalid"] == 3 and out["received"] == 5


def test_ingest_does_not_touch_jobs_already_past_the_new_stage():
    store = FakeStore()
    PL.ingest(store, [raw(1)], SETTINGS, NOW)
    store.jobs[1]["status"] = "applied"
    out = PL.ingest(store, [raw(1, title="Tech Lead")], SETTINGS, NOW)   # would be filtered if still new
    assert out["seen"] == 1 and out["filtered_out"] == 0 and store.jobs[1]["status"] == "applied"


# ------------------------------------------------------------------ scoring stage

@pytest.fixture
def profile():
    here = os.path.dirname(__file__)
    with open(os.path.join(here, "..", "..", "config", "profile.example.json"), encoding="utf-8") as f:
        return json.load(f)


def test_prepare_batch_returns_prompts_for_claimed_jobs(profile):
    store = FakeStore()
    PL.ingest(store, [raw(1), raw(2)], SETTINGS, NOW)
    store.jobs[2]["posted_at"] = NOW
    out = PL.prepare_batch(store, profile, limit=5)
    assert out["claimed"] == 2
    item = out["items"][1]
    assert item["job_id"] == 2 and "untrusted_job_posting_" in item["user"] and item["max_tokens"] > 0
    assert PL.prepare_batch(store, profile, 5)["claimed"] == 2  # fake store has no lease; real SQL tested separately


GOOD = json.dumps({
    "scores": {"technical": 80, "experience": 70, "behavioral": 60, "career": 90},
    "gates": {"eligibility": {"verdict": "PASS", "note": ""}, "language": {"verdict": "PASS", "note": ""},
              "location": {"verdict": "PASS", "note": ""}},
    "deadline": None, "posting_language": "English", "yoe": {"min": 2, "max": 4},
    "strengths": ["Go match"], "gaps": ["no Kafka"], "reason": "Strong."})


def test_score_one_records_the_computed_outcome():
    store = FakeStore()
    PL.ingest(store, [raw(1)], SETTINGS, NOW)
    out = PL.score_one(store, 1, GOOD, threshold=70)
    assert out["status"] == "shortlisted" and out["score"] == 78 and out["above_threshold"]
    assert store.recorded[0][1]["next_status"] == "shortlisted"


def test_score_one_below_threshold_stays_scored():
    store = FakeStore()
    PL.ingest(store, [raw(1)], SETTINGS, NOW)
    assert PL.score_one(store, 1, GOOD, threshold=90)["status"] == "scored"


def test_invalid_reply_counts_a_failure_and_stores_nothing():
    store = FakeStore()
    PL.ingest(store, [raw(1)], SETTINGS, NOW)
    for expected in (1, 2):
        with pytest.raises(PL.ScoreFailure) as e:
            PL.score_one(store, 1, "I cannot help with that", threshold=70)
        assert e.value.attempts == expected and "no JSON" in str(e.value)
    assert store.recorded == []


# ------------------------------------------------------------------ digest

def row(i, **over):
    r = {"id": i, "title": f"Backend Engineer {i}", "company": "Acme", "location": "Pune", "score": 80 - i,
         "verdict": "strong", "scam_risk": "low", "scam_flags": [], "deadline": None, "gates": {},
         "strengths": ["Go and Postgres match"], "gaps": ["no Kafka"], "canonical_url": f"https://acme.com/jobs/{i}",
         "posted_at": NOW}
    r.update(over)
    return r


def test_digest_formats_matches_with_the_important_warnings():
    today = date(2026, 10, 11)
    rows = [row(1, deadline=date(2026, 10, 14)),
            row(2, scam_risk="medium", scam_flags=[{"code": "FREE_EMAIL_CONTACT"}]),
            row(3, gates={"eligibility": {"verdict": "UNKNOWN", "note": "posting is silent"},
                          "language": {"verdict": "PASS", "note": ""}}),
            row(4, posted_at=NOW - timedelta(days=60))]
    d = PL.format_digest(rows, today)
    t = d["text"]
    assert d["count"] == 4 and d["ids"] == [1, 2, 3, 4]
    assert "4 new matches" in t and "1. 79 strong | Backend Engineer 1 @ Acme (Pune)" in t
    assert "deadline 2026-10-14" in t and "FREE_EMAIL_CONTACT" in t and "eligibility unknown: posting is silent" in t
    assert "posted over 30 days ago" in t and "https://acme.com/jobs/1" in t and "+ Go and Postgres match" in t
    assert "language" not in t.replace("language unknown", "")  # PASS gates are not mentioned


def test_digest_is_empty_when_there_is_nothing_new():
    assert PL.format_digest([]) == {"count": 0, "ids": [], "text": "", "html": "", "alert": ""}


def test_digest_truncates_to_fit_telegram_and_only_marks_shown_jobs():
    rows = [row(i, strengths=["x" * 150], gaps=["y" * 150]) for i in range(1, 60)]
    d = PL.format_digest(rows, date(2026, 10, 11))
    assert len(d["text"]) <= 4096 and 0 < d["count"] < 59
    assert d["ids"] == list(range(1, d["count"] + 1))   # only what was shown gets marked as sent
    assert "more (run /status)" in d["text"]


def test_sweep_wraps_the_database_function():
    assert PL.sweep(FakeStore()) == {"expired": 3}


# ------------------------------------------------------------------ reassess

def test_reassess_restores_jobs_that_are_no_longer_high_risk_and_keeps_real_scams_blocked():
    store = FakeStore()
    jobs = [raw(1, description="Build APIs in Go. 2-4 years of experience."),
            raw(2, description="Pay a refundable registration fee of Rs 500 to confirm your slot."),
            raw(3, title="Tech Lead", description="Lead the team.")]
    PL.ingest(store, jobs, SETTINGS, NOW)
    for jid in (1, 2, 3):                      # pretend an old, stricter rule set blocked all three
        store.jobs[jid].update(status="scam_blocked", scam_risk="high", filter_reason="scam risk: high")
    out = PL.reassess_blocked(store, SETTINGS, NOW)
    assert out == {"still_blocked": 1, "restored": 1, "restored_then_filtered": 1}
    assert store.jobs[1]["status"] == "new" and store.jobs[1]["filter_reason"] is None
    assert store.jobs[2]["status"] == "scam_blocked"
    assert store.jobs[3]["status"] == "filtered_out" and "excluded word" in store.jobs[3]["filter_reason"]


def test_reassess_with_nothing_blocked_is_a_no_op():
    assert PL.reassess_blocked(FakeStore(), SETTINGS, NOW) == {"still_blocked": 0, "restored": 0, "restored_then_filtered": 0}


def test_digest_html_variant_escapes_third_party_text():
    rows = [row(1, title="C++ <Engineer> & Co", canonical_url="https://acme.com/jobs?a=1&b=2")]
    d = PL.format_digest(rows, date(2026, 10, 11))
    assert "<Engineer>" in d["text"] and "&lt;Engineer&gt; &amp; Co" in d["html"]
    assert "<" not in d["html"] and "a=1&amp;b=2" in d["html"]


# ------------------------------------------------------------------ budget guard + usage logging

USAGE = {"input_tokens": 1200, "output_tokens": 340, "cache_creation_input_tokens": 1300, "cache_read_input_tokens": 0}


def test_score_one_logs_the_real_usage_with_the_model():
    store = FakeStore()
    PL.ingest(store, [raw(1)], SETTINGS, NOW)
    PL.score_one(store, 1, GOOD, 70, usage=USAGE, model="claude-haiku-5-5")
    assert store.usage == [{"provider": "anthropic", "purpose": "score", "model": "claude-haiku-5-5", "job_id": 1,
                            "usage": {"input_tokens": 1200, "output_tokens": 340, "cache_write_tokens": 1300,
                                      "cache_read_tokens": 0}, "ok": True, "note": None}]


def test_an_unusable_reply_is_still_billed_so_it_is_logged_as_not_ok():
    store = FakeStore()
    PL.ingest(store, [raw(1)], SETTINGS, NOW)
    with pytest.raises(PL.ScoreFailure):
        PL.score_one(store, 1, "not json", 70, usage=USAGE, model="claude-haiku-5-5")
    assert len(store.usage) == 1 and store.usage[0]["ok"] is False and store.usage[0]["usage"]["output_tokens"] == 340
    assert store.recorded == []


def test_missing_usage_is_logged_as_zero_not_skipped_or_crashed():
    store = FakeStore()
    PL.ingest(store, [raw(1)], SETTINGS, NOW)
    PL.score_one(store, 1, GOOD, 70)
    assert store.usage[0]["usage"] == {"input_tokens": 0, "output_tokens": 0, "cache_write_tokens": 0, "cache_read_tokens": 0}


def test_prepare_batch_claims_nothing_when_a_cap_is_reached(profile):
    store = FakeStore()
    PL.ingest(store, [raw(1), raw(2)], SETTINGS, NOW)
    store.budget.update(blocked=True, blocked_reason="monthly cap reached", month_remaining=0)
    out = PL.prepare_batch(store, profile, limit=10)
    assert out["claimed"] == 0 and out["items"] == [] and out["budget"]["blocked"] is True
    assert out["budget"]["reason"] == "monthly cap reached" and store.claim_limits == []   # the claim was never even attempted


def test_prepare_batch_claims_only_what_the_remaining_budget_can_pay_for(profile):
    store = FakeStore()
    PL.ingest(store, [raw(i) for i in range(1, 6)], SETTINGS, NOW)
    store.budget.update(month_remaining=0.0021, day_remaining=5)     # worst case per job is about 0.0008
    out = PL.prepare_batch(store, profile, limit=10)
    assert store.claim_limits == [out["budget"]["allowed_jobs"]] and 1 <= out["budget"]["allowed_jobs"] <= 2
    assert out["claimed"] == min(2, out["budget"]["allowed_jobs"])


def test_prepare_batch_uses_the_requested_models_price(profile):
    store = FakeStore()
    PL.ingest(store, [raw(1)], SETTINGS, NOW)
    store.pricing = lambda model: {"input_per_mtok": 2, "output_per_mtok": 10, "cache_write_per_mtok": 2.5, "cache_read_per_mtok": 0.1}
    out = PL.prepare_batch(store, profile, limit=10, model="claude-sonnet-5-5")
    assert out["budget"]["worst_case_per_job"] > 0.01          # sonnet-class pricing is roughly 20x haiku


def test_digest_adds_a_spend_footer_and_alerts_even_without_matches():
    spend = {"month_spent": 8.5, "monthly_cap": 10, "warn": True, "blocked": False, "blocked_reason": None}
    d = PL.format_digest([row(1)], date(2026, 10, 11), spend)
    assert d["count"] == 1 and "Claude spend this month: $8.50 of $10.00" in d["text"] and "85%" in d["alert"]
    quiet = PL.format_digest([], date(2026, 10, 11), spend)
    assert quiet["count"] == 0 and quiet["text"] == quiet["alert"] != "" and quiet["ids"] == []


def test_a_quiet_healthy_day_sends_nothing():
    spend = {"month_spent": 0.4, "monthly_cap": 10, "warn": False, "blocked": False, "blocked_reason": None}
    assert PL.format_digest([], date(2026, 10, 11), spend)["text"] == ""


# ------------------------------------------------------------------ plan: golden outputs captured from plan() BEFORE
# the board rotation was added (Phase 7, Task 10). Settings without `boards` / `board_title_words` must keep producing
# exactly these structures, and the store's next_slice must be asked exactly once, with the plain profile key.

class RecordingStore:
    """search_settings + per-key next_slice cursors; records every next_slice call."""

    def __init__(self, settings):
        self.settings, self.cursors, self.calls = settings, {}, []

    def search_settings(self, profile="default"):
        return self.settings

    def next_slice(self, profile, total, batch):
        self.calls.append((profile, total, batch))
        start = self.cursors.get(profile, 0) % total
        self.cursors[profile] = (start + batch) % total
        return start


GOLDEN_LIVE_SETTINGS = {"search_terms": ["backend engineer", "platform engineer"],
                        "locations": ["Pune, India", "Remote", "London, United Kingdom", "Singapore"],
                        "sources": ["indeed", "naukri", "linkedin", "linkedin_apify"],
                        "max_age_days": 7, "exclude_keywords": ["intern"], "exclude_companies": ["Acme"]}
GOLDEN_LIVE_PLAN = {'profile': 'default',
 'total': 28,
 'start': 0,
 'requests': [{'label': 'backend engineer | Pune, India | indeed',
               'source': 'indeed',
               'endpoint': '/search',
               'body': {'site_names': ['indeed'],
                        'search_term': 'backend engineer',
                        'location': 'Pune, India',
                        'results_wanted': 15,
                        'hours_old': 168,
                        'country_indeed': 'india',
                        'linkedin_fetch_description': True}},
              {'label': 'backend engineer | Pune, India | naukri',
               'source': 'naukri',
               'endpoint': '/search',
               'body': {'site_names': ['naukri'],
                        'search_term': 'backend engineer',
                        'location': 'Pune, India',
                        'results_wanted': 15,
                        'hours_old': 168,
                        'country_indeed': 'india',
                        'linkedin_fetch_description': True}},
              {'label': 'backend engineer | Pune, India | linkedin',
               'source': 'linkedin',
               'endpoint': '/search',
               'body': {'site_names': ['linkedin'],
                        'search_term': 'backend engineer',
                        'location': 'Pune, India',
                        'results_wanted': 15,
                        'hours_old': 168,
                        'country_indeed': 'india',
                        'linkedin_fetch_description': True}},
              {'label': 'backend engineer | Pune, India | linkedin_apify',
               'source': 'linkedin_apify',
               'endpoint': '/search/linkedin',
               'body': {'search_term': 'backend engineer',
                        'location': 'Pune, India',
                        'results_wanted': 25,
                        'hours_old': 168,
                        'exclude_keywords': ['intern'],
                        'exclude_companies': ['Acme']}},
              {'label': 'backend engineer | Remote | indeed',
               'source': 'indeed',
               'endpoint': '/search',
               'body': {'site_names': ['indeed'],
                        'search_term': 'backend engineer',
                        'location': 'India',
                        'results_wanted': 15,
                        'hours_old': 168,
                        'country_indeed': 'india',
                        'linkedin_fetch_description': True,
                        'is_remote': True}},
              {'label': 'backend engineer | Remote | naukri',
               'source': 'naukri',
               'endpoint': '/search',
               'body': {'site_names': ['naukri'],
                        'search_term': 'backend engineer',
                        'location': 'India',
                        'results_wanted': 15,
                        'hours_old': 168,
                        'country_indeed': 'india',
                        'linkedin_fetch_description': True,
                        'is_remote': True}}]}
GOLDEN_BARE_SETTINGS = {"search_terms": ["backend engineer"], "locations": ["Pune, India"],
                        "sources": ["indeed", "linkedin_apify"]}
GOLDEN_BARE_PLAN = {'profile': 'default',
 'total': 2,
 'start': 0,
 'requests': [{'label': 'backend engineer | Pune, India | indeed',
               'source': 'indeed',
               'endpoint': '/search',
               'body': {'site_names': ['indeed'],
                        'search_term': 'backend engineer',
                        'location': 'Pune, India',
                        'results_wanted': 15,
                        'hours_old': 168,
                        'country_indeed': 'india',
                        'linkedin_fetch_description': True}},
              {'label': 'backend engineer | Pune, India | linkedin_apify',
               'source': 'linkedin_apify',
               'endpoint': '/search/linkedin',
               'body': {'search_term': 'backend engineer',
                        'location': 'Pune, India',
                        'results_wanted': 25,
                        'hours_old': 168,
                        'exclude_keywords': [],
                        'exclude_companies': []}}]}
GOLDEN_EMPTY_PLAN = {'profile': 'default',
 'total': 0,
 'start': 0,
 'requests': [],
 'note': 'no usable (title, location, source) combinations: check search settings'}


def test_plan_output_is_identical_without_board_sources():
    store = RecordingStore(GOLDEN_LIVE_SETTINGS)
    assert PL.plan(store) == GOLDEN_LIVE_PLAN
    assert store.calls == [("default", 28, 6)]       # one cursor read, plain profile key, no board cursor


def test_plan_output_is_identical_for_settings_without_the_new_keys():
    store = RecordingStore(GOLDEN_BARE_SETTINGS)
    assert PL.plan(store, batch=4) == GOLDEN_BARE_PLAN
    assert store.calls == [("default", 2, 4)]
    assert "board_total" not in GOLDEN_BARE_PLAN and "board_start" not in GOLDEN_BARE_PLAN


def test_plan_with_nothing_usable_keeps_the_old_note():
    store = RecordingStore({"search_terms": [], "locations": ["Pune, India"], "sources": ["indeed"]})
    assert PL.plan(store) == GOLDEN_EMPTY_PLAN
    assert store.calls == []
    # switched-on board systems with no slugs add nothing, so the old note still applies
    store = RecordingStore({"search_terms": [], "locations": ["Pune, India"], "sources": ["indeed", "greenhouse"],
                            "boards": {"greenhouse": []}})
    assert PL.plan(store) == GOLDEN_EMPTY_PLAN and store.calls == []


# ------------------------------------------------------------------ plan: board and feed requests on their own rotation

BOARD_PLAN_SETTINGS = {"search_terms": ["backend engineer"], "locations": ["Pune, India"],
                       "sources": ["indeed", "naukri", "greenhouse", "remotive"],
                       "boards": {"greenhouse": ["s1", "s2", "s3", "s4", "s5"]}, "max_age_days": 7}


def _board_labels(out):
    return [r["label"] for r in out["requests"] if r["endpoint"] == "/fetch/board"]


def test_plan_adds_board_slice_on_its_own_cursor():
    store = RecordingStore(BOARD_PLAN_SETTINGS)
    plain = PL.plan(RecordingStore(dict(BOARD_PLAN_SETTINGS, sources=["indeed", "naukri"])), batch=2)
    out = PL.plan(store, batch=2, board_batch=3)
    assert store.calls == [("default", 2, 2), ("default:boards", 6, 3)]
    assert out["requests"][:2] == plain["requests"]                       # search requests unaffected, and first
    assert [r["endpoint"] for r in out["requests"]] == ["/search", "/search", "/fetch/board", "/fetch/board", "/fetch/board"]
    assert _board_labels(out) == ["boards | greenhouse | s1", "boards | greenhouse | s2", "boards | greenhouse | s3"]
    assert out["total"] == plain["total"] == 2 and out["start"] == 0
    assert out["board_total"] == 6 and out["board_start"] == 0
    assert out["profile"] == "default"
    # the search-only keys keep their meaning; only the two board keys are new
    assert set(out) - set(plain) == {"board_total", "board_start"}


def test_plan_board_cursor_is_per_profile_and_default_batch_is_30():
    store = RecordingStore(BOARD_PLAN_SETTINGS)
    out = PL.plan(store, profile="work")
    assert store.calls == [("work", 2, 6), ("work:boards", 6, 30)]
    assert len(_board_labels(out)) == 6                                    # fewer than 30: all of them, once each
    assert len(set(_board_labels(out))) == 6


def test_plan_with_only_board_sources_returns_board_requests():
    settings = {"search_terms": ["backend engineer"], "locations": ["Pune, India"],
                "sources": ["greenhouse", "remotive"], "boards": {"greenhouse": ["s1", "s2"]}}
    store = RecordingStore(settings)
    out = PL.plan(store)
    assert out["total"] == 0 and out["start"] == 0                          # no search combos, no search cursor movement
    assert store.calls == [("default:boards", 3, 30)]                      # never the plain profile key
    assert _board_labels(out) == ["boards | greenhouse | s1", "boards | greenhouse | s2", "feeds | remotive"]
    assert out["board_total"] == 3 and out["board_start"] == 0
    assert "note" not in out and len(out["requests"]) == 3


def test_plan_with_board_sources_but_no_search_terms_still_returns_boards():
    settings = {"search_terms": [], "locations": ["Pune, India"], "sources": ["indeed", "remotive"]}
    store = RecordingStore(settings)
    out = PL.plan(store)
    assert out["total"] == 0 and out["board_total"] == 1 and _board_labels(out) == ["feeds | remotive"]
    assert store.calls == [("default:boards", 1, 30)]


def test_board_rotation_wraps_without_repeating():
    store = RecordingStore(BOARD_PLAN_SETTINGS)
    seen, starts = [], []
    for _ in range(3):
        out = PL.plan(store, batch=2, board_batch=4)
        labels = _board_labels(out)
        assert len(labels) == len(set(labels)) == 4                        # never a repeat inside one run
        seen += labels
        starts.append(out["board_start"])
    assert starts == [0, 4, 2]                                              # 6 boards, 4 a run: wraps around the end
    assert set(seen) == {"boards | greenhouse | s%d" % i for i in range(1, 6)} | {"feeds | remotive"}
    # the search cursor advanced on its own schedule
    assert store.cursors["default"] == (2 * 3) % 2 and store.cursors["default:boards"] == (4 * 3) % 6


def test_board_batch_larger_than_the_set_returns_each_request_once():
    store = RecordingStore(BOARD_PLAN_SETTINGS)
    out = PL.plan(store, batch=1, board_batch=500)
    assert len(_board_labels(out)) == 6 and len(set(_board_labels(out))) == 6
