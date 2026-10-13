"""The HTTP side of comparing tailoring engines (POST /tailor/compare): every refusal, and that nothing is spent when it refuses.

JobSpy is stubbed, the tailoring service is a fake that records what it was asked: nothing here calls a model."""
import sys
import types

import pytest

pytest.importorskip("fastapi")
sys.modules.setdefault("jobspy", types.SimpleNamespace(scrape_jobs=lambda **kw: None))

from fastapi.testclient import TestClient  # noqa: E402

import app as A  # noqa: E402

JOB = {"id": 7, "title": "Backend Engineer", "company": "Acme", "location": "Pune", "description": "Build APIs in Go.",
       "canonical_url": "https://acme.com/7", "score": 80, "verdict": "good", "strengths": ["Go"], "gaps": ["Kafka"], "status": "shortlisted"}


class Store:
    def __init__(self, **over):
        self.flags = {"tailoring": True, "telegram": False, "digest": True}
        self.job = dict(JOB)
        self.paused = False
        self.__dict__.update(over)

    def features(self): return dict(self.flags)
    def get_job(self, job_id): return dict(self.job, id=job_id) if self.job else None
    def schedule_pause(self): return {"paused": self.paused}


class Service:
    class Pdf:
        up = True
        def health(self): return self.up

    def __init__(self, error=None):
        self.calls, self.error, self.pdf = [], error, Service.Pdf()

    def compare_job(self, job, engines):
        self.calls.append((job["id"], list(engines)))
        if self.error:
            raise self.error
        return {"job_id": job["id"], "folder": "/output/compare/7-acme-backend-engineer", "compare_md": "/output/compare/7-acme-backend-engineer/compare.md",
                "runs": {e: {"status": "compared", "cost_usd": 0.05} for e in engines}, "cost_usd": 0.05 * len(engines)}


@pytest.fixture
def env(monkeypatch):
    def setup(store=None, service=None):
        store, service = store or Store(), service or Service()
        monkeypatch.setattr(A, "get_store", lambda: store)
        monkeypatch.setattr(A, "_tailor_service", lambda s: service)
        client = TestClient(A.app)
        client.store, client.service = store, service
        assert not A._tailor_lock.locked()
        return client
    return setup


def errors(r):
    return " ".join(r.json()["detail"]["errors"])


def test_a_try_out_runs_the_only_writer_by_default(env):
    c = env()
    r = c.post("/tailor/compare", json={"job_id": 7})
    assert r.status_code == 200 and c.service.calls == [(7, ["v2"])]
    assert r.json()["compare_md"].endswith("compare.md") and list(r.json()["runs"]) == ["v2"] and not A._tailor_lock.locked()


def test_one_engine_can_be_asked_for_and_a_duplicate_is_one(env):
    c = env()
    assert c.post("/tailor/compare", json={"job_id": 7, "engines": ["v2", "v2"]}).status_code == 200 and c.service.calls == [(7, ["v2"])]


@pytest.mark.parametrize("engines", [[], ["v3"], ["v1"], ["v1", "v2"], ["v1", "x"], ["V2"]])
def test_unknown_engines_are_refused_before_anything_else(env, engines):
    c = env()
    r = c.post("/tailor/compare", json={"job_id": 7, "engines": engines})
    assert r.status_code == 422 and "engines must be some of v2" in errors(r) and c.service.calls == []


@pytest.mark.parametrize("body", [{}, {"job_id": 0}, {"job_id": "7"}, {"job_id": 7.5}, {"job_id": 2**63}, {"job_id": -1}, {"job_id": True}])
def test_a_bad_job_id_is_a_validation_error(env, body):
    c = env()
    assert c.post("/tailor/compare", json=body).status_code == 422 and c.service.calls == []


def test_it_does_not_run_while_tailoring_is_off(env):
    c = env(Store(flags={"tailoring": False, "telegram": False, "digest": True}))
    r = c.post("/tailor/compare", json={"job_id": 7})
    assert r.status_code == 409 and "tailoring is" in errors(r) and c.service.calls == []


def test_it_does_not_run_during_the_emergency_brake(env):
    c = env(Store(paused=True))
    r = c.post("/tailor/compare", json={"job_id": 7})
    assert r.status_code == 409 and "paused" in errors(r) and c.service.calls == []


def test_a_missing_job_is_a_404_and_a_job_without_a_description_a_422(env):
    assert env(Store(job=None)).post("/tailor/compare", json={"job_id": 99}).status_code == 404
    c = env(Store(job=dict(JOB, description="  ")))
    r = c.post("/tailor/compare", json={"job_id": 7})
    assert r.status_code == 422 and "no description" in errors(r) and c.service.calls == []


def test_a_scheduled_run_that_holds_the_lock_means_try_again_and_nothing_spent(env):
    c = env()
    assert A._tailor_lock.acquire(blocking=False)
    try:
        r = c.post("/tailor/compare", json={"job_id": 7})
    finally:
        A._tailor_lock.release()
    assert r.status_code == 409 and "try again in a minute" in errors(r) and "nothing was spent" in errors(r) and c.service.calls == []


def test_a_pdf_service_that_is_down_is_found_out_before_any_money_is_spent(env):
    c = env()
    c.service.pdf.up = False
    r = c.post("/tailor/compare", json={"job_id": 7})
    assert r.status_code == 409 and "PDF service is not answering" in errors(r) and c.service.calls == [] and not A._tailor_lock.locked()


def test_the_lock_is_released_even_when_the_comparison_fails(env):
    c = env(service=Service(error=RuntimeError("boom")))
    r = c.post("/tailor/compare", json={"job_id": 7})
    assert r.status_code == 503 and "RuntimeError: boom" in errors(r)          # the usual wrapper: a readable error, never a stack trace
    assert not A._tailor_lock.locked()                                           # and the next run is not blocked


def test_an_applied_job_can_be_compared_because_nothing_is_saved(env):
    c = env(Store(job=dict(JOB, status="applied")))
    assert c.post("/tailor/compare", json={"job_id": 7}).status_code == 200


def test_a_scam_blocked_job_is_not_compared_either(env):
    c = env(Store(job=dict(JOB, status="scam_blocked")))
    r = c.post("/tailor/compare", json={"job_id": 7})
    assert r.status_code == 422 and "scam_blocked" in errors(r) and c.service.calls == []


# ---- v1 was retired: no request can select it

class SettingsStore(Store):
    def __init__(self):
        super().__init__()
        self.updates = []

    def update_tailor_settings(self, **fields):
        self.updates.append(fields)
        return dict(fields)


@pytest.mark.parametrize("engine", ["v1", "V1", "v3", ""])
def test_the_settings_endpoint_refuses_any_engine_but_v2_and_changes_nothing(env, engine):
    store = SettingsStore()
    c = env(store)
    r = c.post("/tailor/settings", json={"engine": engine})
    assert r.status_code == 422 and store.updates == []


def test_the_settings_endpoint_accepts_v2(env):
    store = SettingsStore()
    c = env(store)
    r = c.post("/tailor/settings", json={"engine": "v2"})
    assert r.status_code == 200 and store.updates == [{"engine": "v2"}]
