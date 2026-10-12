"""Shared fixtures, and the safety net: no test may open a network connection to anything but this machine
(the same guard as scraper/tests/conftest.py; see docs/testing.md)."""
from __future__ import annotations

import socket
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import create_app
from settings import load_settings
from tests.support import FakeStore, make_env

_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_LOCAL = {"127.0.0.1", "::1", "localhost", "0.0.0.0", "testserver"}


def _is_local(address) -> bool:
    if isinstance(address, (str, bytes)):          # a unix socket path
        return True
    return str(address[0]) in _LOCAL


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    def connect(self, address):
        if not _is_local(address):
            raise RuntimeError(f"a test tried to open a real network connection to {address[0]}: use a fake (see docs/testing.md)")
        return _real_connect(self, address)

    def connect_ex(self, address):
        if not _is_local(address):
            raise RuntimeError(f"a test tried to open a real network connection to {address[0]}: use a fake (see docs/testing.md)")
        return _real_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)


@pytest.fixture
def static_dir(tmp_path) -> Path:
    d = tmp_path / "static"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text("<!doctype html><title>dashboard index</title>", encoding="utf-8")
    (d / "assets" / "index-abc123.js").write_text("console.log('app')", encoding="utf-8")
    (d / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    return d


@pytest.fixture
def settings(tmp_path, static_dir):
    return load_settings(make_env(tmp_path))


@pytest.fixture
def store():
    return FakeStore()


@pytest.fixture
def client(settings, store):
    return TestClient(create_app(settings, store), base_url="http://localhost")
