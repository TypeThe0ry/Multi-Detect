"""Host-policy skips must not hide ordinary socket failures."""

import errno

import conftest as bootstrap
import pytest


@pytest.mark.parametrize("error", [errno.EPERM, errno.EACCES])
def test_loopback_policy_denial_is_an_explicit_skip(monkeypatch, error):
    def denied(*_args):
        raise OSError(error, "host policy denied socket")

    monkeypatch.setattr(bootstrap.socket, "socket", denied)
    with pytest.raises(pytest.skip.Exception, match="denied by host sandbox"):
        bootstrap.require_loopback_sockets.__wrapped__()


def test_loopback_ordinary_os_error_is_not_skipped(monkeypatch):
    def busy(*_args):
        raise OSError(errno.EADDRINUSE, "address already in use")

    monkeypatch.setattr(bootstrap.socket, "socket", busy)
    with pytest.raises(OSError) as error:
        bootstrap.require_loopback_sockets.__wrapped__()
    assert error.value.errno == errno.EADDRINUSE


def test_loopback_requirement_checks_udp_and_tcp_and_closes_both(monkeypatch):
    calls = []

    class Channel:
        def __init__(self, family, kind):
            calls.append((family, kind))

        def __enter__(self):
            return self

        def bind(self, address):
            calls.append(address)

        def __exit__(self, *_args):
            calls.append("closed")

    monkeypatch.setattr(bootstrap.socket, "socket", Channel)
    bootstrap.require_loopback_sockets.__wrapped__()
    assert calls == [
        (bootstrap.socket.AF_INET, bootstrap.socket.SOCK_DGRAM),
        ("127.0.0.1", 0), "closed",
        (bootstrap.socket.AF_INET, bootstrap.socket.SOCK_STREAM),
        ("127.0.0.1", 0), "closed",
    ]
