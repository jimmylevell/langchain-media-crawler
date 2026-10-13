"""SSRF guard. URLs come from web pages and feeds, so an agent could be steered to an internal
address (cloud metadata service, localhost, private network). Only public hosts on web ports pass."""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

import httpx

WEB_PORTS = {None, 80, 443}


class BlockedTarget(httpx.RequestError):
    """The address is not a public web server."""


def ensure_public(url: str) -> None:
    """Raises BlockedTarget unless every address the host resolves to is public.
    Called before each request and for each redirect hop (httpx request hook)."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise BlockedTarget(f"keine gültige http(s)-URL: {url}")
    if parts.port not in WEB_PORTS:
        raise BlockedTarget(f"Port {parts.port} nicht erlaubt: {url}")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or 443, type=socket.SOCK_STREAM)
    except OSError:
        return  # unresolvable: the request itself fails with a normal connection error
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        ip = getattr(ip, "ipv4_mapped", None) or ip
        if not ip.is_global:
            raise BlockedTarget(f"interne Adresse {ip} nicht erlaubt: {url}")
