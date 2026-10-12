"""board_probe: the shared "which board does this company use" probe (the CLI runs it one request at a time, the
dashboard in parallel)."""
from __future__ import annotations

import io
import json
import threading
import time

import pytest

import board_probe
import sources


class Resp(io.BytesIO):
    status = 200


def opener_for(routes: dict, log: list | None = None, delay: float = 0.0):
    lock = threading.Lock()

    def opener(req, timeout):
        if log is not None:
            with lock:
                log.append((req.full_url, timeout))
        if delay:
            time.sleep(delay)
        body = routes.get(req.full_url)
        if body is None:
            raise OSError("no such host")
        return Resp(json.dumps(body).encode())

    return opener


def url(system, slug):
    return sources.PROBE_URLS[system].format(slug=slug)


ROUTES = {url("greenhouse", "stripe"): {"jobs": [{}, {}]}, url("lever", "stripe"): [{}] * 3,
          url("ashby", "stripeinc"): {"jobs": []}}


def test_parallel_and_sequential_give_the_same_answer_in_the_same_order():
    one = board_probe.boards_find("Stripe Inc", opener_for(ROUTES))
    many = board_probe.boards_find("Stripe Inc", opener_for(ROUTES), workers=8)
    assert one == many
    assert [(m["system"], m["slug"]) for m in many] == [("ashby", "stripeinc"), ("greenhouse", "stripe"), ("lever", "stripe")]


def test_parallel_probing_is_really_concurrent():
    log: list = []
    started = time.monotonic()
    board_probe.boards_find("Acme Corp", opener_for({}, log, delay=0.2), workers=8)
    assert len(log) == 16 and time.monotonic() - started < 1.5          # sequentially this would take 3.2 s


def test_the_timeout_is_passed_to_every_request_and_the_request_count_is_capped():
    log: list = []
    board_probe.boards_find("a b c d e f g h i j", opener_for({}, log), timeout=3, workers=4)
    assert len(log) <= 20 and {t for _, t in log} == {3}


def test_only_the_five_fixed_hosts_are_ever_contacted():
    log: list = []
    board_probe.boards_find("Stripe", opener_for({}, log), workers=8)
    allowed = {template.split("/")[2] for template in sources.PROBE_URLS.values()}
    assert {u.split("/")[2] for u, _ in log} <= allowed


@pytest.mark.parametrize("name", ["evil.com/../x", "a@b", "x y@evil.example", "../../etc/passwd", "a?b=c#d"])
def test_a_name_can_never_steer_a_request_to_another_host_or_path(name):
    log: list = []
    try:
        board_probe.boards_find(name, opener_for({}, log), workers=8)
    except ValueError:
        return
    allowed = {template.split("/")[2] for template in sources.PROBE_URLS.values()}
    for u, _ in log:
        host, *segments = u.split("?")[0].split("/")[2:]
        assert host in allowed and "@" not in u.split("?")[0] and "#" not in u
        assert all(seg not in (".", "..", "") or seg == segments[-1] == "" for seg in segments), u   # no traversal segment


def test_an_error_in_one_probe_is_just_no_match_for_that_probe():
    def opener(req, timeout):
        if "greenhouse" in req.full_url:
            raise RuntimeError("boom")
        return Resp(json.dumps([{}]).encode()) if "lever" in req.full_url else Resp(b"not json")
    assert board_probe.boards_find("stripe", opener, workers=8) == [{"system": "lever", "slug": "stripe", "jobs": 1}]
