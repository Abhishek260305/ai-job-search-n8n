"""Test-suite safety net: no test may open a network connection to anything but this machine.

Real API calls cost money. The tests use fakes, mock transports and the local stub servers (stubs.py), so if
anything ever tries to reach the internet, fail loudly instead of quietly spending credits."""
import socket

import pytest

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
            raise RuntimeError(f"a test tried to open a real network connection to {address[0]}: use a fake or a stub (see docs/testing.md)")
        return _real_connect(self, address)

    def connect_ex(self, address):
        if not _is_local(address):
            raise RuntimeError(f"a test tried to open a real network connection to {address[0]}: use a fake or a stub (see docs/testing.md)")
        return _real_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
