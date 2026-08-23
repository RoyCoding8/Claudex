"""Threaded local pool router for CLIProxyAPI.

Pooled Anthropic requests are retried across every member (subject only to a
request-wide deadline); direct model requests remain transparent passthroughs.
"""
from __future__ import annotations

import json
import logging
import random
import socket
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
from http import HTTPStatus
from itertools import chain
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

from .config import (
    POOLS_FILE, PROXY_API_KEY, PROXY_HOST, PROXY_PORT, ROUTER_API_KEY,
    ROUTER_COOLDOWN_429, ROUTER_COOLDOWN_5XX, ROUTER_COOLDOWN_AUTH,
    ROUTER_COOLDOWN_NETWORK, ROUTER_COOLDOWN_PACED_429, ROUTER_HOST,
    ROUTER_LOG, ROUTER_PORT, ROUTER_START_TIMEOUT,
)

_UPSTREAM_TIMEOUT = 600.0
_UPSTREAM_HEADER_TIMEOUT = 60.0
_POOL_REQUEST_TIMEOUT = 120.0
_MODELS_CACHE_TTL = 30.0
_POOLS_STAT_INTERVAL = 0.25
_ERROR_PEEK_BYTES = 16 * 1024
_COOLDOWN_ON_429_DEFAULT = ROUTER_COOLDOWN_429
_COOLDOWN_ON_5XX = ROUTER_COOLDOWN_5XX
_COOLDOWN_ON_NETERR = ROUTER_COOLDOWN_NETWORK
_COOLDOWN_ON_AUTH = ROUTER_COOLDOWN_AUTH
_COOLDOWN_ON_PACED_429 = ROUTER_COOLDOWN_PACED_429
_AUTH_STATUS = frozenset({401, 403})
_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "content-length",
})
_LOG = logging.getLogger("cx.router")


@dataclass(frozen=True, slots=True)
class _Member:
    model: str
    rpm: int
    priority: int
    limit: int | None = None
    cooldown: float | None = None


@dataclass(frozen=True, slots=True)
class _Pool:
    name: str
    members: tuple[_Member, ...]
    strategy: str = "fill-first"


_STRATEGIES = frozenset({"fill-first", "round-robin"})


class _PoolRegistry:
    """Thread-safe, mtime-backed pool configuration view."""
    def __init__(self, path: Path) -> None:
        self._path, self._lock = path, threading.Lock()
        self._pools: dict[str, _Pool] = {}
        self._mtime_ns, self._last_stat = -1, 0.0

    def get(self, name: str) -> _Pool | None:
        self._refresh_if_changed()
        return self._pools.get(name)

    def names(self) -> list[str]:
        self._refresh_if_changed()
        return sorted(self._pools)

    def _refresh_if_changed(self) -> None:
        now = time.monotonic()
        if now - self._last_stat < _POOLS_STAT_INTERVAL:
            return
        self._last_stat = now
        try:
            mtime_ns = self._path.stat().st_mtime_ns
        except FileNotFoundError:
            with self._lock:
                self._pools, self._mtime_ns = {}, -1
            return
        if mtime_ns == self._mtime_ns:
            return
        with self._lock:
            if mtime_ns == self._mtime_ns:
                return
            try:
                payload = json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                _LOG.warning("pools.json unreadable; retaining prior state: %s", error)
                return
            self._pools, self._mtime_ns = _parse_pools(payload), mtime_ns
            _LOG.info("loaded %d enabled pool(s) from %s", len(self._pools), self._path)


def _coerce_int(value: Any, *, default: int, minimum: int) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


def _coerce_float(value: Any, *, minimum: float) -> float | None:
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value >= minimum else None


def _parse_pools(payload: Any) -> dict[str, _Pool]:
    pools: dict[str, _Pool] = {}
    if not isinstance(payload, dict):
        return pools
    for raw in payload.get("pools", []) or []:
        if not isinstance(raw, dict) or not raw.get("enabled", True):
            continue
        name = str(raw.get("name", "")).strip()
        if not name:
            continue
        members: list[_Member] = []
        for item in raw.get("members", []) or []:
            if not isinstance(item, dict):
                continue
            model = str(item.get("model", "")).strip()
            if not model:
                continue
            rpm = _coerce_int(item.get("rpm"), default=1, minimum=1)
            if "limit" in item:
                limit = _coerce_int(item.get("limit"), default=0, minimum=1) or None
            else:
                limit = rpm if "rpm" in item else None
            members.append(_Member(
                model, rpm, _coerce_int(item.get("priority"), default=0, minimum=0),
                limit, _coerce_float(item.get("cooldown"), minimum=1.0),
            ))
        if members:
            strategy = str(raw.get("strategy") or "fill-first").strip().lower()
            if strategy not in _STRATEGIES:
                strategy = "fill-first"
            pools[name] = _Pool(name, tuple(members), strategy)
    return pools


class _RateLimiter:
    _WINDOW = 60.0
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    @classmethod
    def _prune(cls, hits: deque[float], now: float) -> None:
        while hits and hits[0] <= now - cls._WINDOW:
            hits.popleft()

    def has_capacity(self, model: str, limit: int | None) -> bool:
        if limit is None:
            return True
        now = time.monotonic()
        with self._lock:
            hits = self._hits.get(model)
            if not hits:
                return True
            self._prune(hits, now)
            return len(hits) < limit

    def record(self, model: str, limit: int | None) -> None:
        """Count at dispatch, never after the upstream response arrives."""
        if limit is None:
            return
        now = time.monotonic()
        with self._lock:
            hits = self._hits.setdefault(model, deque())
            self._prune(hits, now)
            hits.append(now)


class _CooldownTable:
    def __init__(self) -> None:
        self._until: dict[str, float] = {}
        self._lock = threading.Lock()

    def is_ready(self, model: str) -> bool:
        expiry = self._until.get(model)
        return expiry is None or expiry <= time.monotonic()

    def cooldown(self, model: str, seconds: float, reason: str) -> None:
        seconds = max(1.0, min(seconds, 1800.0))
        with self._lock:
            self._until[model] = time.monotonic() + seconds
        _LOG.info("cooldown %.0fs on %s (%s)", seconds, model, reason)

    def clear(self, model: str) -> None:
        with self._lock:
            self._until.pop(model, None)


def _weighted_choice(members: list[_Member]) -> _Member:
    if len(members) == 1:
        return members[0]
    point, total = random.uniform(0, sum(m.rpm for m in members)), 0.0
    for member in members:
        total += member.rpm
        if point <= total:
            return member
    return members[-1]


class _Rotation:
    def __init__(self) -> None:
        self._cursors: dict[str, int] = {}
        self._lock = threading.Lock()

    def take(self, pool: str) -> int:
        with self._lock:
            index = self._cursors.get(pool, 0)
            self._cursors[pool] = index + 1
            return index


def _pick_member(pool: _Pool, cooldowns: _CooldownTable, exclude: set[str],
                 limiter: _RateLimiter | None = None,
                 rotation: _Rotation | None = None) -> _Member | None:
    """Choose one untried member without letting cooldowns suppress fallback.

    Availability is selected before anything else: ready/unpaced members are
    preferred, followed by paced members, then cooling members. Within that,
    fill-first pools drain strict priority tiers (weighted-random by rpm inside
    a tier) while round-robin pools ignore tiers and weights, cycling through
    members in config order. Therefore a pool only reports exhaustion after
    every distinct member has genuinely been dispatched.
    """
    candidates = [member for member in pool.members if member.model not in exclude]
    if not candidates:
        return None
    selected = ready = [member for member in candidates if cooldowns.is_ready(member.model)]
    if ready and limiter is not None:
        capacity = [m for m in ready if limiter.has_capacity(m.model, m.limit)]
        if capacity:
            selected = capacity
    if not selected:
        # Every untried member is cooling. Cooldown is a last-resort preference,
        # never a hard exclusion.
        selected = candidates
    if pool.strategy == "round-robin":
        cursor = rotation.take(pool.name) if rotation is not None else 0
        return selected[cursor % len(selected)]
    top = min(member.priority for member in selected)
    return _weighted_choice([member for member in selected if member.priority == top])


@dataclass(slots=True)
class _UpstreamResponse:
    status: int
    reason: str
    headers: list[tuple[str, str]]
    body_iter: Iterable[bytes]
    connection: HTTPConnection
    _closed: bool = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.connection.close()
        except OSError:
            pass


def _forward_to_upstream(method: str, path: str, headers: dict[str, str], body: bytes) -> _UpstreamResponse:
    conn = HTTPConnection(PROXY_HOST, PROXY_PORT, timeout=_UPSTREAM_TIMEOUT)
    try:
        conn.request(method, path, body=body, headers=headers)
        if conn.sock is not None:
            conn.sock.settimeout(_UPSTREAM_HEADER_TIMEOUT)
        response = conn.getresponse()
    except BaseException:
        conn.close()
        raise
    body_socket = conn.sock or getattr(getattr(response.fp, "raw", None), "_sock", None)
    if body_socket is not None:
        body_socket.settimeout(_UPSTREAM_TIMEOUT)

    def iterate() -> Iterable[bytes]:
        try:
            while True:
                chunk = response.read1(65536)
                if not chunk:
                    remaining = getattr(response, "length", None)
                    if remaining not in (None, 0):
                        raise HTTPException("upstream closed before declared Content-Length")
                    return
                yield chunk
        finally:
            try:
                response.close()
            except OSError:
                pass

    return _UpstreamResponse(response.status, response.reason or "", [
        (key, value) for key, value in response.getheaders() if key.lower() not in _HOP_BY_HOP
    ], iterate(), conn)


class _ModelListCache:
    def __init__(self) -> None:
        self._payload: dict[str, Any] | None = None
        self._fetched_at = 0.0
        self._lock = threading.Lock()

    def get(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._payload is not None and now - self._fetched_at < _MODELS_CACHE_TTL:
            return self._payload
        with self._lock:
            now = time.monotonic()
            if self._payload is not None and now - self._fetched_at < _MODELS_CACHE_TTL:
                return self._payload
            payload = self._fetch()
            if payload is not None:
                self._payload, self._fetched_at = payload, now
            # Never cache a failure: retain a prior good payload, or give callers
            # a temporary empty result that will be retried on their next request.
            return self._payload or {"object": "list", "data": []}

    def _fetch(self) -> dict[str, Any] | None:
        conn = HTTPConnection(PROXY_HOST, PROXY_PORT, timeout=5.0)
        try:
            conn.request("GET", "/v1/models", headers={"Authorization": f"Bearer {PROXY_API_KEY}"})
            response = conn.getresponse()
            data = response.read()
            if response.status != 200:
                _LOG.warning("upstream /v1/models returned %s", response.status)
                return None
            payload = json.loads(data.decode("utf-8"))
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise json.JSONDecodeError("invalid models payload", "", 0)
            return payload
        except (OSError, HTTPException, json.JSONDecodeError) as error:
            _LOG.warning("upstream /v1/models fetch failed: %s", error)
            return None
        finally:
            try:
                conn.close()
            except OSError:
                pass


class _RouterHandler(BaseHTTPRequestHandler):
    server_version, protocol_version = "cx-router/1.1", "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        _LOG.debug("%s - %s", self.address_string(), format % args)

    def log_error(self, format: str, *args: Any) -> None:  # noqa: A002
        _LOG.info("%s - %s", self.address_string(), format % args)

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path in {"/", "/health", "/-/ready", "/-/health"}:
            self._send_json(200, {"status": "ok"})
        elif path == "/v1/models":
            if self._require_auth():
                self._handle_models()
        else:
            self._send_json(404, {"error": {"message": f"unknown path: {path}"}})

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/v1/messages":
            if self._require_auth():
                self._handle_messages(count_tokens=False)
        elif path == "/v1/messages/count_tokens":
            if self._require_auth():
                self._handle_messages(count_tokens=True)
        elif path in {"/v1/chat/completions", "/v1/completions"}:
            if self._require_auth():
                self._handle_passthrough(path)
        else:
            self._send_json(404, {"error": {"message": f"unknown path: {path}"}})

    def _require_auth(self) -> bool:
        header = (self.headers.get("Authorization") or "").strip()
        api_key = self.headers.get("x-api-key", "").strip()
        if ((header.startswith("Bearer ") and header[7:] == ROUTER_API_KEY)
                or api_key == ROUTER_API_KEY):
            return True
        self._send_json(401, {"error": {"message": "invalid api key"}})
        return False

    def _handle_models(self) -> None:
        cache: _ModelListCache = self.server.models_cache  # type: ignore[attr-defined]
        registry: _PoolRegistry = self.server.pools  # type: ignore[attr-defined]
        payload = dict(cache.get())
        data = list(payload.get("data") or [])
        upstream_ids = {str(model.get("id", "")).strip() for model in data if isinstance(model, dict)}
        for name in registry.names():
            if name not in upstream_ids:
                data.append({"id": name, "object": "model", "created": int(time.time()), "owned_by": "pool"})
        payload["object"], payload["data"] = "list", data
        self._send_json(200, payload)

    def _handle_messages(self, *, count_tokens: bool) -> None:
        body = self._read_body()
        if body is None:
            return
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except json.JSONDecodeError as error:
            self._send_json(400, {"error": {"message": f"invalid JSON body: {error}"}})
            return
        if not isinstance(payload, dict):
            self._send_json(400, {"error": {"message": "body must be a JSON object"}})
            return
        requested_model = str(payload.get("model") or "").strip()
        if not requested_model:
            self._send_json(400, {"error": {"message": "missing 'model' field"}})
            return
        registry: _PoolRegistry = self.server.pools  # type: ignore[attr-defined]
        upstream_path = "/v1/messages/count_tokens" if count_tokens else "/v1/messages"
        pool = registry.get(requested_model)
        if pool is None:
            self._forward_once(upstream_path, dict(self.headers), body)
        else:
            self._forward_pool(pool, upstream_path, body, payload)

    def _forward_pool(self, pool: _Pool, path: str, body: bytes, payload: dict[str, Any]) -> None:
        cooldowns: _CooldownTable = self.server.cooldowns  # type: ignore[attr-defined]
        limiter: _RateLimiter = self.server.limiter  # type: ignore[attr-defined]
        rotation: _Rotation = self.server.rotation  # type: ignore[attr-defined]
        request_id = uuid.uuid4().hex[:12]
        deadline = time.monotonic() + _POOL_REQUEST_TIMEOUT
        tried: set[str] = set()
        failures: list[dict[str, Any]] = []
        attempt = 0
        while len(tried) < len(pool.members) and time.monotonic() < deadline:
            member = _pick_member(pool, cooldowns, tried, limiter, rotation)
            if member is None:
                break
            attempt += 1
            tried.add(member.model)
            limiter.record(member.model, member.limit)
            rewritten = _rewrite_model(body, payload, member.model)
            _LOG.info("request=%s attempt=%d pool=%s member=%s", request_id, attempt, pool.name, member.model)
            try:
                upstream = _forward_to_upstream("POST", path, _upstream_headers(self.headers, rewritten), rewritten)
                retry = _classify_retry(upstream)
            except (OSError, HTTPException) as error:
                cooldowns.cooldown(member.model, _COOLDOWN_ON_NETERR, "network")
                failures.append({"category": "network"})
                _LOG.warning("request=%s pool=%s member=%s category=network error=%s", request_id, pool.name, member.model, error)
                continue
            if retry is None:
                cooldowns.clear(member.model)
                self._stream_upstream(upstream, request_id=request_id, member=member.model)
                return
            category, delay = retry
            if category == "rate_limit":
                _log_rate_limit_headers(member.model, upstream.headers)
                if not _has_retry_after(upstream.headers):
                    delay = member.cooldown if member.cooldown is not None else (
                        _COOLDOWN_ON_PACED_429 if member.limit is not None else delay
                    )
            prefix = _read_error_prefix(upstream)
            upstream.close()
            cooldowns.cooldown(member.model, delay, category)
            failures.append({"category": category, "status": upstream.status})
            _LOG.warning(
                "request=%s pool=%s member=%s status=%d category=%s cooldown=%.1fs upstream=%r",
                request_id, pool.name, member.model, upstream.status, category, delay, prefix,
            )
        timed_out = time.monotonic() >= deadline and len(tried) < len(pool.members)
        _LOG.warning("request=%s pool=%s exhausted attempts=%d timed_out=%s", request_id, pool.name, len(tried), timed_out)
        self._send_json(503, {"error": {
            "message": "router: no pool member succeeded", "type": "pool_exhausted",
            "request_id": request_id, "attempts": failures,
        }})

    def _handle_passthrough(self, path: str) -> None:
        body = self._read_body()
        if body is not None:
            self._forward_once(path, dict(self.headers), body)

    def _forward_once(self, path: str, headers: dict[str, str], body: bytes) -> None:
        try:
            upstream = _forward_to_upstream("POST", path, _upstream_headers(headers, body), body)
        except (OSError, HTTPException) as error:
            self._send_json(502, {"error": {"message": f"router: upstream unreachable: {error}"}})
            return
        self._stream_upstream(upstream)

    def _read_body(self) -> bytes | None:
        length = self.headers.get("Content-Length")
        if length is None:
            return b""
        try:
            size = int(length)
        except ValueError:
            self._send_json(400, {"error": {"message": "invalid Content-Length"}})
            return None
        if size < 0 or size > 128 * 1024 * 1024:
            self._send_json(413, {"error": {"message": "request too large"}})
            return None
        return self.rfile.read(size) if size else b""

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _stream_upstream(self, upstream: _UpstreamResponse, *, request_id: str | None = None,
                         member: str | None = None) -> None:
        streaming = _is_event_stream(upstream.headers)
        try:
            if not streaming:
                # A non-SSE response is deliberately buffered so HTTP/1.1 clients
                # can distinguish a complete response from a truncated connection.
                try:
                    chunks = list(upstream.body_iter)
                except (OSError, HTTPException) as error:
                    _LOG.warning("request=%s member=%s upstream body failed before response: %s", request_id, member, error)
                    self._send_json(502, {"error": {"message": "router: upstream response interrupted"}})
                    return
                body = b"".join(chunks)
                self._send_upstream_headers(upstream, len(body))
                try:
                    self.wfile.write(body)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            self._send_upstream_headers(upstream, None)
            sent = False
            sse_buffer = b""
            try:
                for chunk in upstream.body_iter:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    sent = True
                    sse_buffer += chunk
                    while b"\n\n" in sse_buffer:
                        frame, sse_buffer = sse_buffer.split(b"\n\n", 1)
                        if _sse_frame_is_error(frame):
                            _LOG.warning("request=%s member=%s forwarded trailing SSE error", request_id, member)
            except (OSError, HTTPException) as error:
                _LOG.warning("request=%s member=%s upstream SSE interrupted after_bytes=%s: %s", request_id, member, sent, error)
                try:
                    self.wfile.write(b'event: error\ndata: {"type":"error","error":{"message":"upstream stream interrupted"}}\n\n')
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
            except (BrokenPipeError, ConnectionResetError):
                return
        finally:
            upstream.close()

    def _send_upstream_headers(self, upstream: _UpstreamResponse, content_length: int | None) -> None:
        reason = HTTPStatus(upstream.status).phrase if 100 <= upstream.status < 600 else upstream.reason
        self.send_response(upstream.status, reason)
        for key, value in upstream.headers:
            self.send_header(key, value)
        if content_length is not None:
            self.send_header("Content-Length", str(content_length))
        self.send_header("Connection", "close")
        self.end_headers()


def _rewrite_model(original_body: bytes, parsed: dict[str, Any], real_model: str) -> bytes:
    payload = dict(parsed)
    payload["model"] = real_model
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _upstream_headers(incoming: Any, body: bytes) -> dict[str, str]:
    output: dict[str, str] = {}
    for key in incoming.keys() if hasattr(incoming, "keys") else []:
        lower = key.lower()
        if lower in _HOP_BY_HOP or lower in {"authorization", "x-api-key", "host"}:
            continue
        value = incoming[key] if hasattr(incoming, "__getitem__") else incoming.get(key)
        if value is not None:
            output[key] = value
    output["Content-Type"] = output.get("Content-Type", "application/json")
    output["Content-Length"] = str(len(body))
    output["Host"] = f"{PROXY_HOST}:{PROXY_PORT}"
    output["Authorization"] = f"Bearer {PROXY_API_KEY}"
    output["x-api-key"] = PROXY_API_KEY
    output.setdefault("anthropic-version", "2023-06-01")
    output.setdefault("Accept", "application/json, text/event-stream")
    return output


def _has_retry_after(headers: list[tuple[str, str]]) -> bool:
    return any(key.lower() == "retry-after" for key, _ in headers)


def _log_rate_limit_headers(model: str, headers: list[tuple[str, str]]) -> None:
    values = [f"{key}={value}" for key, value in headers
              if key.lower() == "retry-after" or key.lower().startswith("x-ratelimit")]
    _LOG.info("rate-limit headers from %s: %s", model, ", ".join(values) or "none advertised")


def _retry_after_seconds(headers: list[tuple[str, str]], default: float) -> float:
    for key, value in headers:
        if key.lower() != "retry-after":
            continue
        try:
            delay = float(value)
        except ValueError:
            try:
                date = parsedate_to_datetime(value)
            except (TypeError, ValueError, OverflowError):
                return default
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            delay = date.timestamp() - time.time()
        return max(1.0, min(delay, 1800.0))
    return default


def _peek_sse_frame(upstream: _UpstreamResponse) -> bytes:
    """Inspect only the first complete SSE envelope and preserve all bytes."""
    iterator, chunks, total = iter(upstream.body_iter), [], 0
    try:
        while total < _ERROR_PEEK_BYTES:
            chunk = next(iterator)
            chunks.append(chunk)
            total += len(chunk)
            joined = b"".join(chunks)
            boundary = joined.find(b"\n\n")
            if boundary >= 0:
                upstream.body_iter = chain(chunks, iterator)
                return joined[:boundary]
    except StopIteration:
        pass
    upstream.body_iter = chain(chunks, iterator)
    return b""


def _sse_frame_is_error(frame: bytes) -> bool:
    event = ""
    data_lines: list[str] = []
    for line in frame.decode("utf-8", errors="replace").replace("\r\n", "\n").split("\n"):
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if event == "error":
        return True
    if not data_lines:
        return False
    try:
        payload = json.loads("\n".join(data_lines))
    except json.JSONDecodeError:
        return False
    return isinstance(payload, dict) and payload.get("type") == "error"


def _classify_retry(upstream: _UpstreamResponse) -> tuple[str, float] | None:
    if 200 <= upstream.status < 300:
        if _is_event_stream(upstream.headers) and _sse_frame_is_error(_peek_sse_frame(upstream)):
            return "stream_error", _COOLDOWN_ON_5XX
        return None
    if upstream.status == 429:
        return "rate_limit", _retry_after_seconds(upstream.headers, _COOLDOWN_ON_429_DEFAULT)
    if upstream.status in _AUTH_STATUS:
        return "auth", _COOLDOWN_ON_AUTH
    if 400 <= upstream.status < 500:
        return "rejected", _COOLDOWN_ON_5XX
    return "upstream", _COOLDOWN_ON_5XX


def _is_event_stream(headers: list[tuple[str, str]]) -> bool:
    return any(key.lower() == "content-type" and "text/event-stream" in value.lower()
               for key, value in headers)


def _read_error_prefix(upstream: _UpstreamResponse, max_bytes: int = _ERROR_PEEK_BYTES) -> bytes:
    """Return a bounded diagnostic prefix for logs; never expose it to clients."""
    data = bytearray()
    try:
        for chunk in upstream.body_iter:
            data.extend(chunk[:max_bytes - len(data)])
            if len(data) >= max_bytes:
                break
    except (OSError, HTTPException):
        pass
    return bytes(data)


class _RouterServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address: tuple[str, int]) -> None:
        super().__init__(address, _RouterHandler)
        self.pools = _PoolRegistry(POOLS_FILE)
        self.cooldowns, self.limiter = _CooldownTable(), _RateLimiter()
        self.rotation, self.models_cache = _Rotation(), _ModelListCache()


def _wait_upstream(deadline: float) -> None:
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((PROXY_HOST, PROXY_PORT), timeout=0.5):
                return
        except OSError:
            time.sleep(0.3)
    raise RuntimeError(f"CLIProxyAPI unreachable at {PROXY_HOST}:{PROXY_PORT} — start it before the router.")


def _configure_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if ROUTER_LOG:
        try:
            ROUTER_LOG.parent.mkdir(parents=True, exist_ok=True)
            handler: logging.Handler = logging.FileHandler(ROUTER_LOG, encoding="utf-8")
        except OSError:
            handler = logging.StreamHandler(sys.stderr)
    else:
        handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.handlers = [handler]


def run_forever() -> int:
    _configure_logging()
    _LOG.info("router starting on %s:%d, forwarding to %s:%d", ROUTER_HOST, ROUTER_PORT, PROXY_HOST, PROXY_PORT)
    _wait_upstream(time.monotonic() + ROUTER_START_TIMEOUT)
    server = _RouterServer((ROUTER_HOST, ROUTER_PORT))
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        _LOG.info("router stopping (KeyboardInterrupt)")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run_forever())
