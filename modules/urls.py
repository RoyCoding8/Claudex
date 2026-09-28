from __future__ import annotations

import ipaddress
from typing import Any
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener, urlopen


def _url_host(host: str) -> str:
    if host.startswith("[") and host.endswith("]"):
        return host
    try:
        if ipaddress.ip_address(host).version == 6:
            return f"[{host}]"
    except ValueError:
        pass
    return host


def http_url(host: str, port: int, path: str = "") -> str:
    return f"http://{_url_host(host)}:{port}{path}"


def is_local_host(host: str) -> bool:
    candidate = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    normalized = candidate.rstrip(".").lower()
    if normalized == "localhost" or normalized.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(candidate)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        return address.is_loopback or address.is_unspecified
    except ValueError:
        return False


def open_http_url(request: Request, *, timeout: float) -> Any:
    if is_local_host(urlsplit(request.full_url).hostname or ""):
        return build_opener(ProxyHandler({})).open(request, timeout=timeout)
    return urlopen(request, timeout=timeout)
