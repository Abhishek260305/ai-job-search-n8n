"""The data routes, against a fake store: what they return, what they refuse, and how store errors become HTTP."""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

import files
from app import create_app
from models import JobsQuery
from store import Conflict, Forbidden, Invalid, NotFound, Unavailable
from tests.support import PASSWORD, WRITE, job_row


@pytest.fixture
def client(settings, store):
    c = TestClient(create_app(settings, store), base_url="http://localhost")
    assert c.post("/api/login", json={"password": PASSWORD}).status_code == 204
    return c


def last_query(store) -> JobsQuery:
    return store.calls[-1][1]


# ---------------------------------------------------------------- summary

def test_summary(client):
    r = client.get("/api/summary")
    assert r.status_code == 200
    body = r.json()
    assert body["cards"]["found"] == 15 and body["threshold"] == 60
    assert [s["step"] for s in body["funnel"]][0] == "found"
    assert r.headers["cache-control"] == "no-store"


# ---------------------------------------------------------------- the jobs table

def test_jobs_defaults(client, store):
    r = client.get("/api/jobs")
    assert r.status_code == 200
    body = r.json()
    assert (body["total"], body["page"], body["page_size"]) == (2, 1, 50)
    assert [j["id"] for j in body["items"]] == [1, 2]
    assert last_query(store) == JobsQuery()


def test_jobs_query_string_reaches_the_store(client, store):
    r = client.get("/api/jobs", params=[("tab", "all"), ("q", "acme"), ("status", "interview"), ("status", "offer"),
                                        ("source", "lever"), ("min_score", "60"), ("remote", "true"),
                                        ("posted_days", "7"), ("sort", "company"), ("order", "asc"),
                                        ("page", "3"), ("page_size", "25")])
    assert r.status_code == 200
    assert last_query(store) == JobsQuery(tab="all", q="acme", status=["interview", "offer"], source=["lever"],
                                          min_score=60, remote=True, posted_days=7, sort="company", order="asc",
                                          page=3, page_size=25)
    assert (r.json()["page"], r.json()["page_size"]) == (3, 25)


@pytest.mark.parametrize("query", ["page_size=1000", "sort=salary", "status=hired", "source=monster", "tab=x",
                                   "min_score=101", "page=0", "nonsense=1", "q=" + "x" * 101])
def test_bad_jobs_queries_are_422_before_the_store(client, store, query):
    r = client.get(f"/api/jobs?{query}")
    assert r.status_code == 422
    assert store.calls == []


def test_extra_columns_from_the_store_are_not_leaked(client, store):
    store.rows = [job_row(1, resume_path="/output/secret/resume.pdf")]
    item = client.get("/api/jobs").json()["items"][0]
    assert "resume_path" not in item


# ---------------------------------------------------------------- one job

def test_job_detail(client):
    r = client.get("/api/jobs/1")
    assert r.status_code == 200
    body = r.json()
    assert body["description"] == "about the job" and body["sources_detail"][0]["source"] == "indeed"


def test_missing_job_is_404(client):
    r = client.get("/api/jobs/99")
    assert r.status_code == 404 and r.json() == {"detail": "job not found"}


@pytest.mark.parametrize("bad", ["0", "-1", "abc", str(2**63)])
def test_bad_job_ids_are_422(client, store, bad):
    assert client.get(f"/api/jobs/{bad}").status_code == 422
    assert store.calls == []


# ---------------------------------------------------------------- the status change

def patch(client, job_id=1, headers=WRITE, **body):
    body = {"status": "applied", "expected_status": "shortlisted"} | body
    return client.patch(f"/api/jobs/{job_id}/status", json=body, headers=headers)


def test_status_change(client, store):
    r = patch(client, note="sent through the site")
    assert r.status_code == 200 and r.json() == {"id": 1, "status": "applied", "event_id": 42}
    assert store.calls[-1] == ("move_status", 1, "applied", "shortlisted", "sent through the site")


def test_a_change_that_wrote_nothing_has_no_entry_to_undo(client, store):
    store.event_id = None
    assert patch(client).json() == {"id": 1, "status": "applied", "event_id": None}


def test_status_change_needs_the_write_header(client, store):
    assert patch(client, headers={}).status_code == 403
    assert store.calls == []


def test_status_change_needs_a_session(settings, store):
    anonymous = TestClient(create_app(settings, store), base_url="http://localhost")
    assert patch(anonymous).status_code == 401
    assert store.calls == []


def test_a_stale_status_is_409_with_the_current_status(client, store):
    store.fail = Conflict("job 1 changed", "interview")
    r = patch(client)
    assert r.status_code == 409
    assert r.json()["current_status"] == "interview" and "changed elsewhere" in r.json()["detail"]


@pytest.mark.parametrize("error, code, detail", [
    (Forbidden("job 1 is scam-blocked"), 403, "job 1 is scam-blocked"),
    (NotFound("no job 1"), 404, "job not found"),
    (Invalid("note is longer than 500 characters"), 422, "note is longer than 500 characters"),
    (Unavailable("the database is not available right now"), 503, "the database is not available right now"),
])
def test_store_errors_become_http_codes(client, store, error, code, detail):
    store.fail = error
    r = patch(client)
    assert r.status_code == code and r.json() == {"detail": detail}


@pytest.mark.parametrize("body", [{"status": "drafted"}, {"status": "hired"}, {"expected_status": "nope"},
                                  {"note": "x" * 501}, {"extra": True}])
def test_bad_status_changes_are_422_before_the_store(client, store, body):
    assert patch(client, **body).status_code == 422
    assert store.calls == []


def test_a_database_outage_on_a_read_is_503(client, store):
    store.fail = Unavailable("the database is not available right now")
    assert client.get("/api/summary").status_code == 503
    assert client.get("/api/jobs").status_code == 503


# ---------------------------------------------------------------- the PDFs

@pytest.fixture
def pdf(settings, store):
    path = settings.output_dir / "applications" / "1-company" / "resume.pdf"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"%PDF-1.4 fake resume")
    store.files[(1, "resume")] = str(path)
    return path


def test_a_resume_is_served_inline_with_its_own_policy(client, pdf):
    r = client.get("/api/jobs/1/files/resume")
    assert r.status_code == 200 and r.content == b"%PDF-1.4 fake resume"
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"] == 'inline; filename="1-resume.pdf"'
    assert r.headers["content-security-policy"] == files.PDF_CSP, "the PDF policy, not the app's"
    assert r.headers["x-content-type-options"] == "nosniff"


def test_no_file_is_404(client):
    assert client.get("/api/jobs/1/files/cover_letter").status_code == 404


def test_a_refused_path_is_404(client, store, settings, tmp_path):
    outside = tmp_path / "elsewhere.pdf"
    outside.write_bytes(b"%PDF-1.4")
    store.files[(1, "resume")] = str(outside)
    assert client.get("/api/jobs/1/files/resume").status_code == 404


def test_an_unknown_file_kind_is_422(client, store):
    assert client.get("/api/jobs/1/files/summary.md").status_code == 422
    assert client.get("/api/jobs/1/files/..%2F..%2Fetc%2Fpasswd").status_code in (404, 422)
    assert store.calls == []


# ---------------------------------------------------------------- the CSV export

def test_export(client, store):
    r = client.get("/api/export.csv?tab=applied&min_score=50&page=4")
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/csv; charset=utf-8"
    assert re.fullmatch(r'attachment; filename="jobs-\d{4}-\d{2}-\d{2}\.csv"', r.headers["content-disposition"])
    assert r.content.startswith(b"\xef\xbb\xbfid,company,title")
    assert store.calls[-1] == ("export_rows", JobsQuery(tab="applied", min_score=50, page=4))
    assert r.text.count("\r\n") == 3


def test_export_refuses_bad_filters(client, store):
    assert client.get("/api/export.csv?sort=salary").status_code == 422
    assert store.calls == []


# ---------------------------------------------------------------- the schema the frontend types come from

def test_the_schema_documents_the_error_bodies(client):
    schema = client.get("/api/openapi.json").json()
    patch_op = schema["paths"]["/api/jobs/{job_id}/status"]["patch"]
    assert {"200", "403", "404", "409", "422", "503"} <= patch_op["responses"].keys()
    assert "current_status" in schema["components"]["schemas"]["ErrorBody"]["properties"]
    assert "application/pdf" in schema["paths"]["/api/jobs/{job_id}/files/{kind}"]["get"]["responses"]["200"]["content"]
