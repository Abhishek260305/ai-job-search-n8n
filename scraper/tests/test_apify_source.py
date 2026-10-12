import json
from datetime import date

import httpx
import pytest

import apify_source as A

P = A.SearchParams(search_term="backend engineer", location="Bengaluru", results_wanted=20, hours_old=24)

# ---- sample items shaped like each actor's documented output (values are made up) ----
CURIOUS_ITEM = {
    "id": "111", "link": "https://www.linkedin.com/jobs/view/111", "title": "Backend Engineer",
    "companyName": "Acme", "location": "Bengaluru, India", "salaryInfo": ["$17.00", "$19.00"],
    "postedAt": "2026-10-09", "descriptionText": "Build APIs", "applyUrl": "https://acme.com/a",
    "workplaceTypes": ["Remote"],
}
CHEAP_ITEM = {
    "jobId": "222", "jobUrl": "https://www.linkedin.com/jobs/view/222", "jobTitle": "Python Developer",
    "companyName": "Globex", "location": "Pune, India", "salaryInfo": [],
    "publishedAt": "2026-10-09T00:00:00.000Z", "jobDescription": "Python all day",
    "applyUrl": "https://globex.com/a", "yearsOfExperience": [{"years": "5+"}],
}
BEBITY_ITEM = {
    "id": "333", "jobUrl": "https://www.linkedin.com/jobs/view/333", "title": "Data Engineer",
    "companyName": "Initech", "location": "Remote", "workType": "Remote",
    "salaryMin": 70000, "salaryMax": 90000, "salaryCurrency": "EUR",
    "publishedAt": "2026-10-08", "description": "ETL things", "applyUrl": "https://initech.com/a",
}


# ------------------------------------------------------------------ inputs

def test_search_url_has_time_window_and_remote():
    url = A.linkedin_search_url(A.SearchParams("data eng", "Pune", hours_old=24, is_remote=True))
    assert "keywords=data+eng" in url and "f_TPR=r86400" in url and "f_WT=2" in url


def test_every_actor_input_carries_a_result_cap():
    assert A.build_curious(P)["limitPerSource"] == 20
    assert A.build_bebity(P)["rows"] == 20
    assert A.build_cheap(P)["maxItems"] == 150  # actor minimum wins over a smaller request


def test_cheap_cap_follows_request_when_larger_than_minimum():
    big = A.SearchParams("x", "y", results_wanted=300)
    assert A.build_cheap(big)["maxItems"] == 300


def test_enrichment_is_off_by_default():
    assert A.build_curious(P)["scrapeCompany"] is False
    assert A.build_cheap(P)["enrichCompanyData"] is False
    b = A.build_bebity(P)
    assert b["companyProfile"] is False and b["enrichCompany"] is False


def test_cheap_excludes_only_sent_when_present():
    assert "jobTitleExclude" not in A.build_cheap(P)
    p = A.SearchParams("x", "y", exclude_keywords=["intern"], exclude_companies=["Evil Corp"])
    inp = A.build_cheap(p)
    assert inp["jobTitleExclude"] == ["intern"] and inp["companyExclude"] == ["Evil Corp"]


def test_cost_estimates():
    by_key = {a.key: a for a in A.ACTORS}
    assert by_key["curious"].estimate_cost(P) == pytest.approx(0.03)
    assert by_key["cheap"].estimate_cost(P) == pytest.approx(150 * 0.0007 + 0.02)


# ------------------------------------------------------------------ mappers

def test_map_curious():
    j = A.map_curious(CURIOUS_ITEM)
    assert j["source"] == "linkedin" and j["source_job_id"] == "111"
    assert j["title"] == "Backend Engineer" and j["company"] == "Acme"
    assert (j["salary_min"], j["salary_max"], j["salary_currency"]) == (17.0, 19.0, "USD")
    assert j["is_remote"] is True and j["description"] == "Build APIs"
    assert j["raw"] == CURIOUS_ITEM


def test_map_cheap():
    j = A.map_cheap(CHEAP_ITEM)
    assert j["source_job_id"] == "222" and j["title"] == "Python Developer"
    assert j["description"] == "Python all day" and j["salary_min"] is None


def test_map_bebity():
    j = A.map_bebity(BEBITY_ITEM)
    assert j["source_job_id"] == "333" and j["is_remote"] is True
    assert (j["salary_min"], j["salary_max"], j["salary_currency"]) == (70000, 90000, "EUR")


@pytest.mark.parametrize("mapper", [A.map_curious, A.map_cheap, A.map_bebity])
def test_incomplete_items_are_skipped(mapper):
    assert mapper({}) is None
    assert mapper({"id": "1", "jobId": "1", "title": "No company", "jobTitle": "No company"}) is None


def test_salary_parsing():
    assert A.parse_salary_strings(["₹1,200,000"]) == (1200000.0, None, "INR")
    assert A.parse_salary_strings([]) == (None, None, None)
    assert A.parse_salary_strings(["negotiable"]) == (None, None, None)


# ------------------------------------------------------------------ selection

def _accts(*keys):
    by_key = {a.key: a for a in A.ACTORS}
    return [A.Account(by_key[k], f"tok-{k}") for k in keys]


def test_richest_account_first():
    order = A.plan_order(_accts("curious", "cheap", "bebity"),
                         {"curious": 1.0, "cheap": 4.0, "bebity": 2.0}, P, 0)
    assert [a.actor.key for a in order] == ["cheap", "bebity", "curious"]


def test_unaffordable_account_dropped():
    order = A.plan_order(_accts("curious", "cheap"), {"curious": 0.001, "cheap": 4.0}, P, 0)
    assert [a.actor.key for a in order] == ["cheap"]


def test_unknown_balances_rotate_by_day_and_go_last():
    accts = _accts("curious", "cheap", "bebity")
    bal = {"curious": None, "cheap": 3.0, "bebity": None}
    d0 = [a.actor.key for a in A.plan_order(accts, bal, P, 0)]
    d1 = [a.actor.key for a in A.plan_order(accts, bal, P, 1)]
    assert d0 == ["cheap", "curious", "bebity"]
    assert d1 == ["cheap", "bebity", "curious"]


def test_load_accounts_ignores_blank_tokens():
    env = {"APIFY_TOKEN_CURIOUS": " abc ", "APIFY_TOKEN_CHEAP": "", "APIFY_TOKEN_BEBITY": "  "}
    accts = A.load_accounts(env)
    assert [a.actor.key for a in accts] == ["curious"] and accts[0].token == "abc"


# ------------------------------------------------------------------ end to end (mock Apify)

class FakeApify:
    """Minimal Apify API. `behaviour[token]` = 'ok' | 'fail' | 'start_error'."""

    def __init__(self, balances, behaviour, items_by_token):
        self.balances, self.behaviour, self.items = balances, behaviour, items_by_token
        self.requests: list[httpx.Request] = []
        self._n = {t: i for i, t in enumerate(balances)}  # ids must not embed the token

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        token = request.headers["Authorization"].removeprefix("Bearer ")
        path = request.url.path
        if path.endswith("/users/me/limits"):
            bal = self.balances[token]
            if bal is None:
                return httpx.Response(500)
            return httpx.Response(200, json={"data": {"limits": {"maxMonthlyUsageUsd": 5.0},
                                                      "current": {"monthlyUsageUsd": 5.0 - bal}}})
        if request.method == "POST" and path.endswith("/runs"):
            if self.behaviour[token] == "start_error":
                return httpx.Response(402, text="payment required")
            return httpx.Response(201, json={"data": {"id": f"run-{self._n[token]}", "status": "RUNNING",
                                                      "defaultDatasetId": f"ds-{self._n[token]}"}})
        if "/actor-runs/" in path and request.method == "GET":
            status = "FAILED" if self.behaviour[token] == "fail" else "SUCCEEDED"
            return httpx.Response(200, json={"data": {"id": f"run-{self._n[token]}", "status": status,
                                                      "defaultDatasetId": f"ds-{self._n[token]}"}})
        if "/datasets/" in path:
            return httpx.Response(200, json=self.items[token])
        return httpx.Response(404)


def _run(fake, env, **kw):
    client = httpx.Client(transport=httpx.MockTransport(fake))
    return A.search_linkedin(P, env=env, client=client, today=date(2026, 10, 11),
                             sleep=lambda s: None, **kw)


def test_end_to_end_uses_richest_account():
    fake = FakeApify({"tok-a": 1.0, "tok-b": 4.0}, {"tok-a": "ok", "tok-b": "ok"},
                     {"tok-a": [CURIOUS_ITEM], "tok-b": [CHEAP_ITEM, {"junk": True}]})
    out = _run(fake, {"APIFY_TOKEN_CURIOUS": "tok-a", "APIFY_TOKEN_CHEAP": "tok-b"})
    assert out["meta"]["account"] == "cheap"
    assert out["meta"]["items_returned"] == 2 and out["meta"]["items_mapped"] == 1
    assert out["jobs"][0]["title"] == "Python Developer" and out["errors"] == []


def test_run_is_capped_and_token_stays_out_of_urls():
    fake = FakeApify({"tok-a": 4.0}, {"tok-a": "ok"}, {"tok-a": [CURIOUS_ITEM]})
    _run(fake, {"APIFY_TOKEN_CURIOUS": "tok-a"})
    start = next(r for r in fake.requests if r.method == "POST")
    assert float(start.url.params["maxTotalChargeUsd"]) >= 0.02
    assert json.loads(start.content)["limitPerSource"] == 20
    assert all("tok-a" not in str(r.url) for r in fake.requests)


def test_falls_back_to_next_account_when_a_run_fails():
    fake = FakeApify({"tok-a": 4.0, "tok-b": 3.0}, {"tok-a": "fail", "tok-b": "ok"},
                     {"tok-a": [], "tok-b": [BEBITY_ITEM]})
    out = _run(fake, {"APIFY_TOKEN_CURIOUS": "tok-a", "APIFY_TOKEN_BEBITY": "tok-b"})
    assert out["meta"]["account"] == "bebity"
    assert len(out["errors"]) == 1 and out["errors"][0].startswith("curious:")


def test_unknown_balance_does_not_block_search():
    fake = FakeApify({"tok-a": None}, {"tok-a": "ok"}, {"tok-a": [CURIOUS_ITEM]})
    out = _run(fake, {"APIFY_TOKEN_CURIOUS": "tok-a"})
    assert out["meta"]["balance_before_usd"] is None and len(out["jobs"]) == 1


def test_all_accounts_failing_raises_with_reasons():
    fake = FakeApify({"tok-a": 4.0, "tok-b": 4.0}, {"tok-a": "start_error", "tok-b": "fail"},
                     {"tok-a": [], "tok-b": []})
    with pytest.raises(A.ApifyError) as e:
        _run(fake, {"APIFY_TOKEN_CURIOUS": "tok-a", "APIFY_TOKEN_CHEAP": "tok-b"})
    assert "curious" in str(e.value) and "cheap" in str(e.value)


def test_no_tokens_configured():
    with pytest.raises(A.ApifyError, match="no Apify tokens"):
        A.search_linkedin(P, env={})


def test_nobody_can_afford_the_run():
    fake = FakeApify({"tok-a": 0.001}, {"tok-a": "ok"}, {"tok-a": []})
    with pytest.raises(A.ApifyError, match="enough credit"):
        _run(fake, {"APIFY_TOKEN_CURIOUS": "tok-a"})


def test_only_pins_one_account():
    fake = FakeApify({"tok-a": 4.0, "tok-b": 1.0}, {"tok-a": "ok", "tok-b": "ok"},
                     {"tok-a": [CURIOUS_ITEM], "tok-b": [CHEAP_ITEM]})
    env = {"APIFY_TOKEN_CURIOUS": "tok-a", "APIFY_TOKEN_CHEAP": "tok-b"}
    out = _run(fake, env, only="cheap")  # curious is richer, but we pinned cheap
    assert out["meta"]["account"] == "cheap"


def test_only_unconfigured_account_errors():
    with pytest.raises(A.ApifyError, match="not configured"):
        A.search_linkedin(P, env={"APIFY_TOKEN_CURIOUS": "x"}, only="bebity")


@pytest.mark.parametrize("hours,expected", [(1, "r86400"), (24, "r86400"), (25, "r604800"),
                                            (72, "r604800"), (168, "r604800"), (169, "r2592000")])
def test_published_bucket_only_emits_values_the_actors_accept(hours, expected):
    assert A.published_bucket(hours) == expected


def test_recency_filter_sent_to_cheap_and_bebity():
    p = A.SearchParams("x", "y", hours_old=72)
    assert A.build_cheap(p)["publishedAt"] == "r604800"
    assert A.build_bebity(p)["publishedAt"] == "r604800"


# ------------------------------------------------------------------ runaway protection

class ChargingApify(FakeApify):
    """Like FakeApify, but a finished run lowers the account's balance, as a real run does."""

    def __init__(self, *a, charge=0.0, hang=(), **kw):
        super().__init__(*a, **kw)
        self.charge, self.hang = charge, set(hang)
        self.aborts = 0

    def __call__(self, request):
        token = request.headers["Authorization"].removeprefix("Bearer ")
        path = request.url.path
        if request.method == "POST" and "/abort" in path:
            self.aborts += 1
            self.requests.append(request)
            return httpx.Response(200, json={"data": {}})
        if "/actor-runs/" in path and request.method == "GET" and token in self.hang:
            self.requests.append(request)
            return httpx.Response(200, json={"data": {"id": "run", "status": "RUNNING", "defaultDatasetId": "ds"}})
        resp = super().__call__(request)
        if "/datasets/" in path and self.balances.get(token) is not None:
            self.balances[token] -= self.charge          # the run is billed when its results are produced
        return resp

    def starts(self):
        return [r for r in self.requests if r.method == "POST" and r.url.path.endswith("/runs")]


ALL3 = {"APIFY_TOKEN_CURIOUS": "tok-a", "APIFY_TOKEN_CHEAP": "tok-b", "APIFY_TOKEN_BEBITY": "tok-c"}


def test_a_search_never_starts_more_than_two_runs_even_with_three_failing_accounts():
    fake = ChargingApify({"tok-a": 4, "tok-b": 3, "tok-c": 2}, {"tok-a": "fail", "tok-b": "fail", "tok-c": "fail"}, {"tok-a": [], "tok-b": [], "tok-c": []})
    with pytest.raises(A.ApifyError) as e:
        _run(fake, ALL3)
    assert len(fake.starts()) == 2, "at most MAX_ATTEMPTS runs can be billed for one search"
    assert e.value.started and len(e.value.attempts) == 2 and [a["account"] for a in e.value.attempts] == ["curious", "cheap"]
    assert all(a["estimated_cost_usd"] > 0 for a in e.value.attempts)


def test_a_rejected_start_costs_nothing_and_is_not_counted_as_billed():
    fake = ChargingApify({"tok-a": 4, "tok-b": 3}, {"tok-a": "start_error", "tok-b": "fail"}, {"tok-a": [], "tok-b": []})
    with pytest.raises(A.ApifyError) as e:
        _run(fake, {"APIFY_TOKEN_CURIOUS": "tok-a", "APIFY_TOKEN_CHEAP": "tok-b"})
    assert [a["account"] for a in e.value.attempts] == ["cheap"], "only the run that actually started may have been billed"


def test_a_search_that_cannot_start_anywhere_has_no_billed_attempts():
    fake = ChargingApify({"tok-a": 4}, {"tok-a": "start_error"}, {"tok-a": []})
    with pytest.raises(A.ApifyError) as e:
        _run(fake, {"APIFY_TOKEN_CURIOUS": "tok-a"})
    assert e.value.attempts == [] and not e.value.started


def test_a_hung_run_is_aborted_after_its_timeout_and_polling_stops():
    fake = ChargingApify({"tok-a": 4}, {"tok-a": "ok"}, {"tok-a": []}, hang=["tok-a"])
    with pytest.raises(A.ApifyError, match="timed out") as e:
        _run(fake, {"APIFY_TOKEN_CURIOUS": "tok-a"})
    polls = [r for r in fake.requests if r.method == "GET" and "/actor-runs/" in r.url.path]
    assert fake.aborts == 1 and len(polls) <= 130, "it polls for a bounded time, then aborts the run"
    assert e.value.started and len(e.value.attempts) == 1


def test_the_real_cost_is_measured_from_the_balance_change():
    fake = ChargingApify({"tok-a": 9.94}, {"tok-a": "ok"}, {"tok-a": [CURIOUS_ITEM]}, charge=0.047)
    out = _run(fake, {"APIFY_TOKEN_CURIOUS": "tok-a"})
    assert out["meta"]["actual_cost_usd"] == 0.047 and out["meta"]["estimated_cost_usd"] == 0.03
    assert out["meta"]["failed_attempts"] == []


def test_an_unreadable_balance_leaves_the_actual_cost_unknown_not_zero():
    fake = ChargingApify({"tok-a": None}, {"tok-a": "ok"}, {"tok-a": [CURIOUS_ITEM]})
    assert _run(fake, {"APIFY_TOKEN_CURIOUS": "tok-a"})["meta"]["actual_cost_usd"] is None


def test_the_failed_first_attempt_is_reported_even_when_the_second_succeeds():
    fake = ChargingApify({"tok-a": 4, "tok-b": 3}, {"tok-a": "fail", "tok-b": "ok"}, {"tok-a": [], "tok-b": [BEBITY_ITEM]}, charge=0.02)
    out = _run(fake, {"APIFY_TOKEN_CURIOUS": "tok-a", "APIFY_TOKEN_BEBITY": "tok-b"})
    assert [a["account"] for a in out["meta"]["failed_attempts"]] == ["curious"]


def test_worst_case_cost_is_the_dearest_charge_cap_for_every_allowed_attempt():
    caps = {k: A.charge_cap(next(x for x in A.ACTORS if x.key == k), P) for k in ("curious", "cheap", "bebity")}
    assert caps["cheap"] > caps["curious"], "the cheap actor bills a 150-result minimum per run"
    assert A.worst_case_cost(P, ALL3) == round(max(caps.values()) * 2, 4)
    assert A.worst_case_cost(P, {"APIFY_TOKEN_CURIOUS": "tok-a"}) == round(caps["curious"] * 2, 4)
    assert A.worst_case_cost(P, {}) == 0.0
    assert A.worst_case_cost(P, ALL3, max_attempts=1) == round(max(caps.values()), 4)


def test_a_run_starts_only_if_its_charge_limit_fits_the_remaining_budget():
    fake = ChargingApify({"tok-a": 4, "tok-b": 3}, {"tok-a": "ok", "tok-b": "ok"}, {"tok-a": [CURIOUS_ITEM], "tok-b": [CHEAP_ITEM]}, charge=0.02)
    cap_curious = A.charge_cap(next(x for x in A.ACTORS if x.key == "curious"), P)
    out = _run(fake, {"APIFY_TOKEN_CURIOUS": "tok-a", "APIFY_TOKEN_CHEAP": "tok-b"}, budget_left=cap_curious + 0.001)
    assert out["meta"]["account"] == "curious" and len(fake.starts()) == 1


def test_with_too_little_budget_nothing_is_started_and_it_is_a_skip():
    fake = ChargingApify({"tok-a": 4}, {"tok-a": "ok"}, {"tok-a": [CURIOUS_ITEM]})
    out = _run(fake, {"APIFY_TOKEN_CURIOUS": "tok-a"}, budget_left=0.001)
    assert out["jobs"] == [] and "not enough Apify budget" in out["skipped"] and fake.starts() == []


def test_a_cheaper_account_is_used_when_the_first_does_not_fit():
    fake = ChargingApify({"tok-a": 4, "tok-b": 3}, {"tok-a": "ok", "tok-b": "ok"}, {"tok-a": [CHEAP_ITEM], "tok-b": [CURIOUS_ITEM]}, charge=0.02)
    caps = {k: A.charge_cap(next(x for x in A.ACTORS if x.key == k), P) for k in ("curious", "cheap")}
    assert caps["cheap"] > caps["curious"]
    out = _run(fake, {"APIFY_TOKEN_CHEAP": "tok-a", "APIFY_TOKEN_CURIOUS": "tok-b"}, budget_left=caps["curious"] + 0.001)
    assert out["meta"]["account"] == "curious", "cheap was richest but its 150-result minimum does not fit; curious does"
    assert any("cheap: skipped" in e for e in out["errors"])


def test_the_second_attempt_must_also_fit_alongside_the_first():
    """Both runs can bill, so the total of the charge limits of the runs started must fit the budget."""
    fake = ChargingApify({"tok-a": 4, "tok-b": 3}, {"tok-a": "fail", "tok-b": "ok"}, {"tok-a": [], "tok-b": [BEBITY_ITEM]})
    cap = A.charge_cap(next(x for x in A.ACTORS if x.key == "curious"), P)
    with pytest.raises(A.ApifyError) as e:
        _run(fake, {"APIFY_TOKEN_CURIOUS": "tok-a", "APIFY_TOKEN_BEBITY": "tok-b"}, budget_left=cap + 0.001)
    assert len(fake.starts()) == 1, "the first failed run used the budget; a second would not fit, so it was not started"
    assert len(e.value.attempts) == 1 and "bebity: skipped" in str(e.value)


def test_a_balance_that_has_not_moved_yet_is_unknown_not_free():
    """Seen with the real Apify API: read straight after a run, the balance had not changed."""
    fake = ChargingApify({"tok-a": 9.94}, {"tok-a": "ok"}, {"tok-a": [CURIOUS_ITEM]}, charge=0.0)
    assert _run(fake, {"APIFY_TOKEN_CURIOUS": "tok-a"})["meta"]["actual_cost_usd"] is None
