"""Reject URLs that point at private or local networks (SSRF guard).

Every URL that arrives from a Telegram user passes here before the bot
fetches it. The mornye extensions already have their own netguard
(``MORNYE_EXT_ALLOW_PRIVATE_NETWORK``); this guards the bot's own
fetchers: direct downloads, yt-dlp and gallery-dl subprocesses, and the
Pixiv cover fetches.

Behind a fakeip/tun VPN (sing-box & co.) every DNS answer lands in the
reserved 198.18.0.0/15 range, which this guard would otherwise reject;
``ALLOW_FAKEIP=1`` exempts exactly that range.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import socket
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

#: Hostnames the operator may explicitly trust (private CDNs etc.).
ALLOWED_HOSTS: set[str] = set()

#: sing-box/mihomo default IPv4 fakeip range.
_FAKEIP_RANGE = ipaddress.ip_network("198.18.0.0/15")


def _allow_fakeip() -> bool:
    """Read per call: ``.env`` is loaded after this module's import."""
    # shortcut: only the default inet4 range; a custom sing-box
    # inet4_range/inet6_range is still rejected.
    return os.environ.get("ALLOW_FAKEIP", "").strip() == "1"


def _is_private_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def reject_private_target(url: str, *, port: int | None = None) -> None:
    """Raise ``ValueError`` when ``url`` resolves to a private network.

    DNS resolution happens synchronously in a worker thread via
    ``asyncio.to_thread`` at the call sites, so this stays blocking-free
    on the event loop.
    """
    parts = urlparse(url)
    if parts.scheme not in {"http", "https"}:
        raise ValueError("Only http and https links are supported")
    host = parts.hostname
    if not host:
        raise ValueError("The link has no host")
    if host in ALLOWED_HOSTS:
        return
    try:
        infos = socket.getaddrinfo(
            host, port or (443 if parts.scheme == "https" else 80), type=socket.SOCK_STREAM
        )
    except socket.gaierror as exc:
        raise ValueError(f"Cannot resolve {host}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if _allow_fakeip() and ip in _FAKEIP_RANGE:
            continue
        if _is_private_ip(ip):
            logger.warning("Blocked private-network target | host=%s ip=%s", host, ip)
            raise ValueError(
                "This link points at a private or local network address. "
                "It is not downloadable."
            )


async def areject_private_target(url: str, *, port: int | None = None) -> None:
    """Async wrapper: run ``reject_private_target`` in a worker thread."""
    import asyncio

    await asyncio.to_thread(reject_private_target, url, port=port)
