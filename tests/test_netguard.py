"""SSRF guard: fakeip DNS behind sing-box must not trip the bot's own
netguard when ``ALLOW_FAKEIP=1``; real private ranges stay blocked."""
import socket

import pytest

from services import netguard


def _resolve_to(ip: str):
    return lambda host, port, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))
    ]


def test_fakeip_allowed_when_enabled(monkeypatch):
    monkeypatch.setattr(netguard.socket, "getaddrinfo", _resolve_to("198.18.0.57"))
    monkeypatch.setenv("ALLOW_FAKEIP", "1")
    netguard.reject_private_target("https://music.youtube.com/playlist")


def test_fakeip_blocked_when_unset(monkeypatch):
    monkeypatch.setattr(netguard.socket, "getaddrinfo", _resolve_to("198.18.0.57"))
    monkeypatch.delenv("ALLOW_FAKEIP", raising=False)
    with pytest.raises(ValueError):
        netguard.reject_private_target("https://music.youtube.com/playlist")


def test_private_still_blocked_with_fakeip_enabled(monkeypatch):
    monkeypatch.setattr(netguard.socket, "getaddrinfo", _resolve_to("192.168.1.10"))
    monkeypatch.setenv("ALLOW_FAKEIP", "1")
    with pytest.raises(ValueError):
        netguard.reject_private_target("http://router.local/config")
