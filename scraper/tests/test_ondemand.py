"""The run registry of on-demand tailoring: one at a time, a cooldown per job, nothing leaks, nothing escapes."""
import threading

import pytest

import ondemand as OD


class Clock:
    def __init__(self): self.now = 1000.0
    def __call__(self): return self.now


def runs(clock=None, spawn="later"):
    clock = clock or Clock()
    queue = []
    r = OD.OnDemandRuns(clock=clock, spawn=queue.append if spawn == "later" else (lambda fn: fn()))
    return r, clock, queue


def ok(job_id=7, **over):
    return {"job_id": job_id, "status": "drafted", "variant": "tailored", "cost_usd": 0.047, "needs_attention": False, "attention": [],
            "has_cover_letter": True, "pages": 1, "files_dir": "/output/applications/7-x", "audit": {"hard": ["x"]}, "notes": ["n"],
            "requirements": {"matched": 5, "bridged": 1, "gap": 0}, **over}


def test_a_started_run_is_running_until_it_finishes_and_then_reports_a_clean_result():
    r, clock, queue = runs()
    assert r.status(7) == {"state": "idle"}
    started = r.start(7, lambda: ok())
    assert started["state"] == "running" and r.status(7)["state"] == "running" and r.running() == 7
    clock.now += 40                                            # the work takes a while ...
    queue.pop()()                                              # ... and happens now
    done = r.status(7)
    assert done["state"] == "done" and done["result"]["status"] == "drafted" and done["result"]["cost_usd"] == 0.047
    assert done["finished_at"] > done["started_at"] and r.running() is None


def test_the_result_never_carries_paths_or_the_models_notes():
    r, _, queue = runs()
    r.start(7, lambda: ok())
    queue.pop()()
    result = r.status(7)["result"]
    assert set(result) == {"job_id", "status", "variant", "needs_attention", "attention", "cost_usd", "has_cover_letter", "pages", "requirements"}
    assert "files_dir" not in str(result) and "audit" not in result and "notes" not in result


def test_the_real_services_requirement_keys_are_kept():
    """TailorService reports requirements as matched / bridged / gap (tests/test_tailor_service.py asserts it): the page reads these."""
    assert OD.clean_result(ok(requirements={"matched": 1, "bridged": 0, "gap": 1}))["requirements"] == {"matched": 1, "bridged": 0, "gap": 1}


def test_attention_lines_are_cut_and_requirements_are_numbers_only():
    out = OD.clean_result(ok(attention=["x" * 1000] * 20, requirements={"matched": 3, "evil": "<script>", "gap": 1}))
    assert len(out["attention"]) == 10 and all(len(a) <= 300 for a in out["attention"])
    assert out["requirements"] == {"matched": 3, "gap": 1}


def test_a_second_start_is_refused_while_one_is_running_for_that_job_or_another():
    r, _, queue = runs()
    r.start(7, lambda: ok())
    with pytest.raises(OD.Refused, match="^this job is being tailored right now$"):
        r.start(7, lambda: ok())
    with pytest.raises(OD.Refused, match="another job"):
        r.start(8, lambda: ok(8))
    assert len(queue) == 1, "the refused starts did not even create work"


def test_a_job_just_done_is_left_alone_for_the_cooldown_but_another_job_may_start():
    r, clock, queue = runs()
    r.start(7, lambda: ok())
    queue.pop()()
    clock.now += 30
    with pytest.raises(OD.Refused, match="a moment ago: wait about 91 seconds"):
        r.start(7, lambda: ok())
    assert "a moment ago" in r.refusal(7) and r.refusal(8) is None
    r.start(8, lambda: ok(8))                                  # another job is fine
    queue.pop()()
    clock.now += 200
    assert r.refusal(7) is None
    r.start(7, lambda: ok())                                   # and after the cooldown the first one again


def test_the_cooldown_counts_from_when_the_run_ENDED_not_when_it_started():
    r, clock, queue = runs()
    r.start(7, lambda: ok())
    clock.now += 100                                           # the run itself took 100 seconds
    queue.pop()()
    clock.now += 30                                            # 130 s after the start, but only 30 after the end
    assert r.refusal(7) is not None
    clock.now += OD.COOLDOWN_S - 30                            # exactly the cooldown after the end
    assert r.refusal(7) is None, "the boundary: after exactly the cooldown it may start"


def test_a_failed_run_keeps_what_it_cost_and_it_reaches_the_status():
    r, _, queue = runs()
    def fails():
        raise OD.Failed("the PDF service is down", spent=0.0184)
    r.start(7, fails)
    queue.pop()()
    s = r.status(7)
    assert s["state"] == "failed" and s["cost_usd"] == 0.0184 and s["error"] == "the PDF service is down"


def test_a_long_reason_is_cut():
    assert len(OD.clean_result({"status": "failed", "reason": "x" * 1000})["reason"]) == 300


def test_a_failed_run_is_reported_in_one_line_and_also_waits_out_the_cooldown():
    r, _, queue = runs()

    def boom():
        raise RuntimeError("the PDF service is down\nwith a long trace " + "x" * 500)
    r.start(7, boom)
    queue.pop()()
    s = r.status(7)
    assert s["state"] == "failed" and "\n" not in s["error"] and len(s["error"]) <= 300 and "PDF service is down" in s["error"]
    assert r.refusal(7) is not None


def test_an_http_style_error_shows_its_message():
    class E(Exception):
        detail = {"errors": ["resume not found: run /setup"]}
    r, _, queue = runs()
    def raises():
        raise E()
    r.start(7, raises)
    queue.pop()()
    assert r.status(7)["error"] == "resume not found: run /setup"


def test_a_crash_in_the_worker_cannot_leave_a_run_running_forever():
    r, _, queue = runs()
    def bad():
        raise ValueError("bad")
    r.start(7, bad)
    queue.pop()()
    assert r.status(7)["state"] == "failed" and r.running() is None


def test_only_the_newest_finished_runs_are_kept():
    r, clock, _ = runs(spawn="now")
    for i in range(1, OD.KEEP + 6):
        clock.now += 1000
        r.start(i, lambda i=i: ok(i))
    assert r.status(1) == {"state": "idle"}, "the oldest was forgotten"
    assert r.status(OD.KEEP + 5)["state"] == "done"


def test_the_real_thread_runs_the_work_off_the_callers_thread():
    done = threading.Event()
    where = []
    r = OD.OnDemandRuns()
    def work():
        where.append(threading.current_thread().name)
        done.set()
        return ok()
    r.start(7, work)
    assert done.wait(5) and where == ["tailor-on-demand"]
    for _ in range(100):
        if r.status(7)["state"] == "done":
            break
        threading.Event().wait(0.02)
    assert r.status(7)["state"] == "done"
