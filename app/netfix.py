"""Prefer IPv4 for every outbound connection (Telegram, Gemini, Google News).

Some networks advertise IPv6 but can't actually route it: each request then
tries the IPv6 address first, hangs ~20s until that times out, and only
then falls back to IPv4. For a long-polling bot that makes every update
and reply crawl. When a host has an IPv4 address, this drops its IPv6
results so connections go straight to IPv4; IPv6-only hosts are untouched.

On by default; set PREFER_IPV4=0 in .env to turn it off.
"""
from __future__ import annotations

import os
import socket

_original_getaddrinfo = socket.getaddrinfo
_installed = False


def _ipv4_first_getaddrinfo(host, port, family=0, *args, **kwargs):
    results = _original_getaddrinfo(host, port, family, *args, **kwargs)
    if family not in (0, socket.AF_UNSPEC):
        return results
    ipv4 = [r for r in results if r[0] == socket.AF_INET]
    return ipv4 or results


def install() -> None:
    global _installed
    if _installed or os.environ.get("PREFER_IPV4", "1").strip() == "0":
        return
    socket.getaddrinfo = _ipv4_first_getaddrinfo
    _installed = True
