"""Threaded local pool router for CLIProxyAPI."""
from __future__ import annotations

import hmac
import json
import logging
import random
import select
import socket
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC
from email.utils import parsedate_to_datetime
from http import HTTPStatus
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import chain
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .config import (
    POOLS_FILE,
    PROXY_API_KEY,
    PROXY_HOST,
    PROXY_PORT,
    ROUTER_API_KEY,
    ROUTER_COOLDOWN_5XX,
    ROUTER_COOLDOWN_429,
    ROUTER_COOLDOWN_AUTH,
    ROUTER_COOLDOWN_EMPTY,
    ROUTER_COOLDOWN_NETWORK,
    ROUTER_COOLDOWN_PACED_429,
    ROUTER_HOST,
    ROUTER_LOG,
    ROUTER_POOL_PASSES,
    ROUTER_POOL_TIMEOUT,
    ROUTER_PORT,
    ROUTER_START_TIMEOUT,
)
from .pools import parse_pool_document

_UPSTREAM_TIMEOUT = 600.0
_UPSTREAM_HEADER_TIMEOUT = 60.0
_POOL_REQUEST_TIMEOUT = ROUTER_POOL_TIMEOUT
_POOL_PASSES = ROUTER_POOL_PASSES
_MODELS_CACHE_TTL = 30.0
_POOLS_STAT_INTERVAL = 0.25
_ERROR_PEEK_BYTES = 16 * 1024
_HEAD_PEEK_BYTES = 256 * 1024
_MAX_BODY_BYTES = 128 * 1024 * 1024
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_SWEEP_BACKOFF = 1.0
_HANDLER_TIMEOUT = 30
_now = time.monotonic
_COOLDOWN_ON_429_DEFAULT = ROUTER_COOLDOWN_429
_COOLDOWN_ON_5XX = ROUTER_COOLDOWN_5XX
_COOLDOWN_ON_NETERR = ROUTER_COOLDOWN_NETWORK
_COOLDOWN_ON_AUTH = ROUTER_COOLDOWN_AUTH
_COOLDOWN_ON_PACED_429 = ROUTER_COOLDOWN_PACED_429
_COOLDOWN_ON_EMPTY = ROUTER_COOLDOWN_EMPTY
_AUTH_STATUS = frozenset({401, 403})
_POOLED_PATHS = frozenset({"/v1/messages", "/v1/messages/count_tokens",
                           "/v1/responses", "/v1/chat/completions"})
_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "content-length",
})
_STRIPPED = frozenset({"authorization", "x-api-key", "host", "accept-encoding", "expect"})
_LOG = logging.getLogger("cx.router")


def _status_phrase(status: int, fallback: str) -> str:
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return fallback or ""


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


class _InFlight:
    """Live dispatch count per member, the ordering key for least-busy pools."""
    def __init__(self) -> None:
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()

    def count(self, model: str) -> int:
        with self._lock:
            return self._counts.get(model, 0)

    @contextmanager
    def hold(self, model: str) -> Iterator[None]:
        with self._lock:
            self._counts[model] = self._counts.get(model, 0) + 1
        try:
            yield
        finally:
            with self._lock:
                if (remaining := self._counts.get(model, 1) - 1) > 0:
                    self._counts[model] = remaining
                else:
                    self._counts.pop(model, None)


class _PoolRegistry:
    """Thread-safe, mtime-backed pool configuration view."""
    def __init__(self, path: Path) -> None:
        self._path, self._lock = path, threading.Lock()
        self._pools: dict[str, _Pool] = {}
        self._mtime_ns, self._last_stat = -1, 0.0

    def get(self, name: str) -> _Pool | None:
        self._refresh_if_changed()
        with self._lock:
            return self._pools.get(name)

    def names(self) -> list[str]:
        self._refresh_if_changed()
        with self._lock:
            return sorted(self._pools)

    def _refresh_if_changed(self) -> None:
        now = _now()
        if now - self._last_stat < _POOLS_STAT_INTERVAL:
            return
        self._last_stat = now
        try:
            mtime_ns = self._path.stat().st_mtime_ns
        except FileNotFoundError:
            with self._lock:
                self._pools, self._mtime_ns = {}, -1
            return
        except OSError:
            return
        if mtime_ns == self._mtime_ns:
            return
        with self._lock:
            if mtime_ns == self._mtime_ns:
                return
            try:
                payload = json.loads(self._path.read_bytes().decode("utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
                _LOG.warning("pools.json unreadable; retaining prior state: %s", error)
                self._mtime_ns = mtime_ns
                return
            self._pools, self._mtime_ns = _parse_pools(payload), mtime_ns
            _LOG.info("loaded %d enabled pool(s) from %s", len(self._pools), self._path)


def _parse_pools(payload: Any) -> dict[str, _Pool]:
    pools, warnings = parse_pool_document(payload)
    for warning in warnings:
        _LOG.warning("pools.json: %s", warning)
    return {
        pool.name: _Pool(
            pool.name,
            tuple(_Member(
                member.model,
                member.rpm if member.rpm is not None else 1,
                0 if member.priority is None else member.priority,
                member.limit if member.limit is not None else member.rpm,
                member.cooldown,
            ) for member in pool.members),
            pool.strategy,
        )
        for pool in pools if pool.enabled
    }


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
        now = _now()
        with self._lock:
            hits = self._hits.get(model)
            if hits is None:
                return True
            self._prune(hits, now)
            if not hits:
                del self._hits[model]
                return True
            return len(hits) < limit

    def record(self, model: str, limit: int | None) -> None:
        """Count at dispatch, never after the upstream response arrives."""
        if limit is None:
            return
        now = _now()
        with self._lock:
            hits = self._hits.setdefault(model, deque())
            self._prune(hits, now)
            hits.append(now)


class _CooldownTable:
    def __init__(self) -> None:
        self._until: dict[str, float] = {}
        self._lock = threading.Lock()

    def is_ready(self, model: str) -> bool:
        with self._lock:
            expiry = self._until.get(model)
            if expiry is None:
                return True
            if expiry <= _now():
                del self._until[model]
                return True
            return False

    def cooldown(self, model: str, seconds: float, reason: str) -> None:
        seconds = max(1.0, min(seconds, 1800.0))
        with self._lock:
            self._until[model] = _now() + seconds
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
    """Per-pool round-robin cursor over member *identity*."""
    def __init__(self) -> None:
        self._state: dict[str, tuple[int, int]] = {}
        self._lock = threading.Lock()

    def reserve(self, pool: str, size: int,
                choose: Callable[[int], int | None]) -> int | None:
        """Atomically pick a starting index from the cursor and park the cursor past it."""
        if size <= 0:
            return None
        with self._lock:
            known, cursor = self._state.get(pool, (size, 0))
            index = choose(cursor if known == size else 0)
            if index is not None:
                self._state[pool] = (size, (index + 1) % size)
            return index

    def cursor(self, pool: str, size: int) -> int:
        with self._lock:
            known, cursor = self._state.get(pool, (size, 0))
            return cursor if known == size else 0


def _top_tier(members: list[_Member]) -> list[_Member]:
    top = min(member.priority for member in members)
    return [member for member in members if member.priority == top]


def _idlest(members: list[_Member], inflight: _InFlight | None) -> list[_Member]:
    counts = {m.model: inflight.count(m.model) if inflight else 0 for m in members}
    least = min(counts.values())
    return [member for member in members if counts[member.model] == least]


_SELECTORS: dict[str, Callable[[list[_Member], _InFlight | None], _Member]] = {
    "fill-first": lambda selected, inflight: _weighted_choice(_top_tier(selected)),
    "weighted": lambda selected, inflight: _weighted_choice(selected),
    "least-busy": lambda selected, inflight: _weighted_choice(_idlest(selected, inflight)),
}


def _pick_member(pool: _Pool, cooldowns: _CooldownTable, exclude: set[str],
                 limiter: _RateLimiter | None = None,
                 start: int | None = None,
                 inflight: _InFlight | None = None,
                 rotation: _Rotation | None = None) -> _Member | None:
    """Choose one untried member without letting cooldowns suppress fallback.

    Availability is checked before strategy so exhaustion means every member was tried.
    """
    if pool.strategy == "round-robin":
        return _rotate_member(pool, cooldowns, exclude, limiter, start or 0, rotation)
    candidates = [member for member in pool.members if member.model not in exclude]
    if not candidates:
        return None
    selected = ready = [member for member in candidates if cooldowns.is_ready(member.model)]
    if ready and limiter is not None:
        capacity = [m for m in ready if limiter.has_capacity(m.model, m.limit)]
        if capacity:
            selected = capacity
    if not selected:
        selected = candidates
    return _SELECTORS.get(pool.strategy, _SELECTORS["fill-first"])(selected, inflight)


def _rotate_member(pool: _Pool, cooldowns: _CooldownTable, exclude: set[str],
                   limiter: _RateLimiter | None, start: int,
                   rotation: _Rotation | None) -> _Member | None:
    def scan(from_index: int) -> int | None:
        size = len(pool.members)
        order = [pool.members[(from_index + offset) % size] for offset in range(size)]
        order = [member for member in order if member.model not in exclude]
        tiers = (
            lambda m: cooldowns.is_ready(m.model) and (limiter is None or limiter.has_capacity(m.model, m.limit)),
            lambda m: cooldowns.is_ready(m.model),
            lambda m: True,
        )
        for accepts in tiers:
            if member := next((m for m in order if accepts(m)), None):
                return next(i for i, m in enumerate(pool.members) if m.model == member.model)
        return None

    size = len(pool.members)
    index = rotation.reserve(pool.name, size, scan) if rotation is not None else scan(start)
    return pool.members[index] if index is not None else None


@dataclass(slots=True)
class _UpstreamResponse:
    status: int
    reason: str
    headers: list[tuple[str, str]]
    body_iter: Iterable[bytes]
    connection: HTTPConnection
    body_socket: Any = None
    buffered: bytes | None = None
    _closed: bool = False

    def relax_timeout(self, deadline: float | None = None) -> None:
        """Head validation reads on a short leash; a committed body needs the long one."""
        if self.body_socket is None:
            return
        remaining = _UPSTREAM_TIMEOUT if deadline is None else max(1.0, deadline - _now())
        try:
            self.body_socket.settimeout(min(_UPSTREAM_TIMEOUT, remaining))
        except OSError:
            pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.connection.close()
        except OSError:
            pass

    def __enter__(self) -> _UpstreamResponse:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


def _forward_to_upstream(method: str, path: str, headers: dict[str, str], body: bytes,
                         deadline: float | None = None) -> _UpstreamResponse:
    leash = _UPSTREAM_TIMEOUT if deadline is None else max(1.0, deadline - _now())
    conn = HTTPConnection(PROXY_HOST, PROXY_PORT, timeout=min(_UPSTREAM_TIMEOUT, leash))
    try:
        conn.request(method, path, body=body, headers=headers)
        if conn.sock is not None:
            conn.sock.settimeout(min(_UPSTREAM_HEADER_TIMEOUT, leash))
        response = conn.getresponse()
    except BaseException:
        conn.close()
        raise
    body_socket = conn.sock or getattr(getattr(response.fp, "raw", None), "_sock", None)
    if body_socket is not None:
        body_socket.settimeout(min(_UPSTREAM_HEADER_TIMEOUT, leash))

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
    ], iterate(), conn, body_socket)


class _ModelListCache:
    def __init__(self) -> None:
        self._payload: dict[str, Any] | None = None
        self._fetched_at = 0.0
        self._lock = threading.Lock()

    def get(self) -> dict[str, Any]:
        now = _now()
        if self._payload is not None and now - self._fetched_at < _MODELS_CACHE_TTL:
            return self._payload
        with self._lock:
            now = _now()
            if self._payload is not None and now - self._fetched_at < _MODELS_CACHE_TTL:
                return self._payload
            payload = self._fetch()
            if payload is not None:
                self._payload, self._fetched_at = payload, now
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
        except (OSError, HTTPException, json.JSONDecodeError, UnicodeDecodeError) as error:
            _LOG.warning("upstream /v1/models fetch failed: %s", error)
            return None
        finally:
            try:
                conn.close()
            except OSError:
                pass


class _RouterHandler(BaseHTTPRequestHandler):
    server_version, protocol_version = "cx-router/1.1", "HTTP/1.1"
    timeout = _HANDLER_TIMEOUT
    server: _RouterServer

    def log_message(self, format: str, *args: Any) -> None:
        _LOG.debug("%s - %s", self.address_string(), format % args)

    def log_error(self, format: str, *args: Any) -> None:
        _LOG.info("%s - %s", self.address_string(), format % args)

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except TimeoutError:
            self.close_connection = True

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path in {"/", "/health", "/-/ready", "/-/health"}:
            self._send_json(200, {"status": "ok"})
        elif path == "/v1/models" and self._require_auth():
            self._handle_models()
        else:
            self._send_json(404, {"error": {"message": f"unknown path: {path}"}})

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path in _POOLED_PATHS:
            if self._require_auth():
                self._handle_pooled(path)
        elif path == "/v1/completions" and self._require_auth():
            self._handle_passthrough(path)
        else:
            self._send_json(404, {"error": {"message": f"unknown path: {path}"}})

    def _require_auth(self) -> bool:
        key = ROUTER_API_KEY.encode("utf-8")
        header = (self.headers.get("Authorization") or "").strip()
        api_key = self.headers.get("x-api-key", "").strip()
        if ((header.startswith("Bearer ") and hmac.compare_digest(header[7:].encode("utf-8"), key))
                or hmac.compare_digest(api_key.encode("utf-8"), key)):
            return True
        self._send_json(401, {"error": {"message": "invalid api key"}})
        return False

    def _handle_models(self) -> None:
        cache: _ModelListCache = self.server.models_cache
        registry: _PoolRegistry = self.server.pools
        payload = dict(cache.get())
        data = list(payload.get("data") or [])
        upstream_ids = {str(model.get("id", "")).strip() for model in data if isinstance(model, dict)}
        for name in registry.names():
            if name in upstream_ids:
                _LOG.warning("pool %r shadows an upstream model with the same ID; "
                             "requests for it are served by the pool, not the model", name)
            else:
                data.append({"id": name, "object": "model", "created": int(time.time()), "owned_by": "pool"})
        payload["object"], payload["data"] = "list", data
        self._send_json(200, payload)

    def _handle_pooled(self, path: str) -> None:
        body = self._read_body()
        if body is None:
            return
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            self._send_json(400, {"error": {"message": f"invalid JSON body: {error}"}})
            return
        if not isinstance(payload, dict):
            self._send_json(400, {"error": {"message": "body must be a JSON object"}})
            return
        requested_model = str(payload.get("model") or "").strip()
        if not requested_model:
            self._send_json(400, {"error": {"message": "missing 'model' field"}})
            return
        registry: _PoolRegistry = self.server.pools
        pool = registry.get(requested_model)
        if pool is None:
            self._forward_once(path, dict(self.headers), body)
        else:
            self._forward_pool(pool, path, body, payload)

    def _client_disconnected(self) -> bool:
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            if not readable:
                return False
            return self.connection.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            return True

    def _forward_pool(self, pool: _Pool, path: str, body: bytes, payload: dict[str, Any]) -> None:
        cooldowns: _CooldownTable = self.server.cooldowns
        limiter: _RateLimiter = self.server.limiter
        rotation: _Rotation = self.server.rotation
        inflight: _InFlight = self.server.inflight
        request_id = uuid.uuid4().hex[:12]
        deadline = _now() + _POOL_REQUEST_TIMEOUT
        size = len(pool.members)
        start = rotation.cursor(pool.name, size)
        distinct = len({member.model for member in pool.members})
        tried: set[str] = set()
        failures: list[dict[str, Any]] = []
        attempt = 0
        sweep = 1
        while _now() < deadline:
            if len(tried) >= distinct:
                if sweep >= _POOL_PASSES:
                    break
                pause = min(_SWEEP_BACKOFF * sweep, max(0.0, deadline - _now()))
                if pause <= 0:
                    break
                time.sleep(pause)
                sweep += 1
                tried.clear()
            if attempt and self._client_disconnected():
                _LOG.info("request=%s client disconnected during failover", request_id)
                return
            member = _pick_member(pool, cooldowns, tried, limiter, start, inflight,
                                  rotation=rotation if not tried else None)
            if member is None:
                break
            attempt += 1
            tried.add(member.model)
            limiter.record(member.model, member.limit)
            rewritten = _rewrite_model(payload, member.model)
            _LOG.info("request=%s attempt=%d pool=%s member=%s", request_id, attempt, pool.name, member.model)
            with inflight.hold(member.model):
                upstream, streamed = None, False
                try:
                    upstream = _forward_to_upstream(
                        "POST", path, _upstream_headers(self.headers, rewritten), rewritten, deadline)
                    retry = _classify_retry(upstream, path=path, deadline=deadline)
                    if retry is None:
                        streamed = True
                        if self._stream_upstream(upstream, request_id=request_id, member=member.model, deadline=deadline):
                            if 200 <= upstream.status < 300:
                                cooldowns.clear(member.model)
                        else:
                            cooldowns.cooldown(member.model, _COOLDOWN_ON_NETERR, "stream_drop")
                        return
                except (OSError, HTTPException) as error:
                    if streamed:
                        _LOG.warning("request=%s member=%s client lost after commit: %s", request_id, member.model, error)
                        return
                    cooldowns.cooldown(member.model, _COOLDOWN_ON_NETERR, "network")
                    failures.append({"category": "network", "status": None})
                    _LOG.warning("request=%s pool=%s member=%s category=network error=%s", request_id, pool.name, member.model, error)
                    continue
                finally:
                    if upstream is not None and not streamed:
                        upstream.close()
                category, delay = retry
                if category == "rate_limit":
                    _log_rate_limit_headers(member.model, upstream.headers)
                    if not _has_retry_after(upstream.headers):
                        delay = member.cooldown if member.cooldown is not None else (
                            _COOLDOWN_ON_PACED_429 if member.limit is not None else delay
                        )
                prefix = _read_error_prefix(upstream)
                cooldowns.cooldown(member.model, delay, category)
                failures.append({"category": category, "status": upstream.status})
                _LOG.warning(
                    "request=%s pool=%s member=%s status=%d category=%s cooldown=%.1fs upstream=%r",
                    request_id, pool.name, member.model, upstream.status, category, delay, prefix,
                )
        timed_out = _now() >= deadline
        _LOG.warning("request=%s pool=%s exhausted attempts=%d sweeps=%d timed_out=%s", request_id, pool.name, attempt, sweep, timed_out)
        self._send_json(503, {"error": {
            "message": "router: no pool member succeeded", "type": "pool_exhausted",
            "request_id": request_id, "attempts": failures,
        }})

    def _handle_passthrough(self, path: str) -> None:
        body = self._read_body()
        if body is not None:
            self._forward_once(path, dict(self.headers), body)

    def _forward_once(self, path: str, headers: dict[str, str], body: bytes) -> None:
        deadline = _now() + _POOL_REQUEST_TIMEOUT
        try:
            upstream = _forward_to_upstream("POST", path, _upstream_headers(headers, body), body, deadline)
        except (OSError, HTTPException) as error:
            self._send_json(502, {"error": {"message": f"router: upstream unreachable: {error}"}})
            return
        self._stream_upstream(upstream, deadline=deadline)

    def _read_body(self) -> bytes | None:
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            return self._read_chunked_body()
        length = self.headers.get("Content-Length")
        if length is None:
            return b""
        try:
            size = int(length)
        except ValueError:
            self._send_json(400, {"error": {"message": "invalid Content-Length"}})
            return None
        if size < 0 or size > _MAX_BODY_BYTES:
            self._send_json(413, {"error": {"message": "request too large"}})
            return None
        return self.rfile.read(size) if size else b""

    def _read_chunked_body(self) -> bytes | None:
        chunks, total = [], 0
        while True:
            line = self.rfile.readline(65536).strip()
            try:
                size = int(line.split(b";")[0] or b"0", 16)
            except ValueError:
                self._send_json(400, {"error": {"message": "invalid chunked framing"}})
                return None
            if size == 0:
                break
            total += size
            if total > _MAX_BODY_BYTES:
                self._send_json(413, {"error": {"message": "request too large"}})
                return None
            chunks.append(self.rfile.read(size))
            self.rfile.read(2)
        while self.rfile.readline(65536).strip():
            pass
        return b"".join(chunks)

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
                         member: str | None = None, deadline: float | None = None) -> bool:
        """Forward a committed response; False means the member let the body down."""
        upstream.relax_timeout(deadline)
        streaming = _is_event_stream(upstream.headers)
        try:
            if not streaming:
                if upstream.buffered is not None:
                    body = upstream.buffered
                else:
                    try:
                        body = b"".join(upstream.body_iter)
                    except (OSError, HTTPException) as error:
                        _LOG.warning("request=%s member=%s upstream body failed before response: %s", request_id, member, error)
                        self._send_json(502, {"error": {"message": "router: upstream response interrupted"}})
                        return False
                self._send_upstream_headers(upstream, len(body))
                try:
                    self.wfile.write(body)
                    self.wfile.flush()
                except (TimeoutError, BrokenPipeError, ConnectionResetError):
                    pass
                return True
            self._send_upstream_headers(upstream, None)
            sent, clean = False, True
            sse_buffer = b""
            try:
                for chunk in upstream.body_iter:
                    try:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    except (TimeoutError, BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                        _LOG.info("request=%s member=%s client disconnected after_bytes=%s", request_id, member, sent)
                        return True
                    sent = True
                    sse_buffer += chunk
                    while b"\n\n" in sse_buffer:
                        frame, sse_buffer = sse_buffer.split(b"\n\n", 1)
                        if _sse_frame_is_error(frame):
                            clean = False
                            _LOG.warning("request=%s member=%s forwarded trailing SSE error", request_id, member)
            except (OSError, HTTPException) as error:
                _LOG.warning("request=%s member=%s upstream SSE interrupted after_bytes=%s: %s", request_id, member, sent, error)
                try:
                    self.wfile.write(b'event: error\ndata: {"type":"error","error":{"message":"upstream stream interrupted"}}\n\n')
                    self.wfile.flush()
                except (TimeoutError, BrokenPipeError, ConnectionResetError):
                    pass
                return False
            return clean
        finally:
            upstream.close()

    def _send_upstream_headers(self, upstream: _UpstreamResponse, content_length: int | None) -> None:
        reason = _status_phrase(upstream.status, upstream.reason)
        self.send_response(upstream.status, reason)
        for key, value in upstream.headers:
            if key.lower() in _RESPONSE_IDENTITY_HEADERS:
                continue
            self.send_header(key, value)
        if content_length is not None:
            self.send_header("Content-Length", str(content_length))
        self.send_header("Connection", "close")
        self.end_headers()


_RESPONSE_IDENTITY_HEADERS = frozenset({"date", "server"})


def _rewrite_model(parsed: dict[str, Any], real_model: str) -> bytes:
    payload = dict(parsed)
    payload["model"] = real_model
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _upstream_headers(incoming: Any, body: bytes) -> dict[str, str]:
    output: dict[str, str] = {}
    for key in incoming.keys() if hasattr(incoming, "keys") else []:
        lower = key.lower()
        if lower in _HOP_BY_HOP or lower in _STRIPPED:
            continue
        value = incoming[key] if hasattr(incoming, "__getitem__") else incoming.get(key)
        if value is not None:
            output[key] = value
    output["Content-Type"] = output.get("Content-Type", "application/json")
    output["Content-Length"] = str(len(body))
    output["Accept-Encoding"] = "identity"
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
                date = date.replace(tzinfo=UTC)
            delay = date.timestamp() - time.time()
        return max(1.0, min(delay, 1800.0))
    return default


def _sse_parts(frame: bytes) -> tuple[str, Any]:
    """Name one SSE envelope by its `event:` line, falling back to `data.type`."""
    event, data_lines = "", []
    for line in frame.decode("utf-8", errors="replace").replace("\r\n", "\n").split("\n"):
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    raw = "\n".join(data_lines).strip()
    try:
        payload = json.loads(raw) if raw and raw != "[DONE]" else None
    except json.JSONDecodeError:
        payload = None
    if not event and isinstance(payload, dict):
        event = str(payload.get("type") or "")
    return event, payload


def _sse_event(frame: bytes) -> str:
    return _sse_parts(frame)[0]


def _sse_frame_is_error(frame: bytes) -> bool:
    return _sse_event(frame) in _ERROR_EVENTS


def _chat_delta_has_content(payload: Any) -> bool:
    """Chat frames carry no `event:` line, so content is judged from the delta itself."""
    if not isinstance(payload, dict):
        return False
    choice = next(iter(payload.get("choices") or ()), None)
    delta = (choice or {}).get("delta") or {}
    return any(delta.get(key) for key in ("content", "reasoning_content", "tool_calls"))


@dataclass(frozen=True)
class _Grammar:
    content_events: frozenset[str]
    verdicts: dict[str, tuple[str, float]]
    body_field: str
    payload_probe: Callable[[Any], bool] | None = None

    def has_content(self, event: str, payload: Any) -> bool:
        return event in self.content_events or bool(
            self.payload_probe and self.payload_probe(payload))


_STREAM_ERROR = ("stream_error", _COOLDOWN_ON_5XX)
_EMPTY = ("empty", _COOLDOWN_ON_EMPTY)

_ANTHROPIC_GRAMMAR = _Grammar(
    frozenset({"content_block_start", "content_block_delta"}),
    {"error": _STREAM_ERROR, "message_stop": _EMPTY},
    "content",
)
_RESPONSES_GRAMMAR = _Grammar(
    frozenset({"response.output_item.added", "response.output_text.delta",
               "response.reasoning_summary_text.delta", "response.reasoning_text.delta",
               "response.function_call_arguments.delta"}),
    {"error": _STREAM_ERROR, "response.failed": _STREAM_ERROR,
     "response.incomplete": _EMPTY, "response.completed": _EMPTY},
    "output",
)
_CHAT_GRAMMAR = _Grammar(
    frozenset(),
    {"error": _STREAM_ERROR},
    "choices",
    _chat_delta_has_content,
)

_GRAMMARS = {
    "/v1/messages": _ANTHROPIC_GRAMMAR,
    "/v1/responses": _RESPONSES_GRAMMAR,
    "/v1/chat/completions": _CHAT_GRAMMAR,
}
_ERROR_EVENTS = frozenset({"error", "response.failed"})


def _grammar_for(path: str) -> _Grammar:
    return _GRAMMARS.get(path.removesuffix("/count_tokens"), _ANTHROPIC_GRAMMAR)


def _validate_stream_head(upstream: _UpstreamResponse, deadline: float,
                          grammar: _Grammar) -> tuple[str, float] | None:
    """Hold an SSE response until it proves it carries content."""
    iterator, chunks, buffered, total = iter(upstream.body_iter), [], b"", 0
    outcome: tuple[str, float] | None = None
    try:
        while total < _HEAD_PEEK_BYTES and _now() < deadline:
            chunk = next(iterator)
            chunks.append(chunk)
            total += len(chunk)
            buffered += chunk
            events = []
            while b"\n\n" in buffered:
                frame, buffered = buffered.split(b"\n\n", 1)
                events.append(_sse_parts(frame))
            if any(grammar.has_content(event, payload) for event, payload in events):
                break
            if outcome := next((grammar.verdicts[e] for e, _ in events
                                if e in grammar.verdicts), None):
                break
    except StopIteration:
        outcome = "empty", _COOLDOWN_ON_EMPTY
    except (OSError, HTTPException):
        outcome = "truncated", _COOLDOWN_ON_NETERR
    upstream.body_iter = chain(chunks, iterator)
    return outcome


def _validate_body(upstream: _UpstreamResponse, path: str,
                   grammar: _Grammar) -> tuple[str, float] | None:
    """Judge a non-SSE 2xx while failover is still possible."""
    upstream.relax_timeout()
    chunks, total = [], 0
    try:
        for chunk in upstream.body_iter:
            total += len(chunk)
            if total > _MAX_RESPONSE_BYTES:
                return "oversized", _COOLDOWN_ON_5XX
            chunks.append(chunk)
    except (OSError, HTTPException):
        return "truncated", _COOLDOWN_ON_NETERR
    body = b"".join(chunks)
    upstream.body_iter, upstream.buffered = iter((body,)), body
    if not body.strip():
        return "empty", _COOLDOWN_ON_EMPTY
    try:
        payload = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return "malformed", _COOLDOWN_ON_EMPTY
    if not isinstance(payload, dict):
        return "malformed", _COOLDOWN_ON_EMPTY
    if path.endswith("/count_tokens"):
        return None
    content = payload.get(grammar.body_field)
    if not isinstance(content, list) or not content:
        return "empty", _COOLDOWN_ON_EMPTY
    return None


_RETRYABLE_4XX = frozenset({408, 409, 425})


def _classify_retry(upstream: _UpstreamResponse, *, path: str, deadline: float) -> tuple[str, float] | None:
    if 200 <= upstream.status < 300:
        grammar = _grammar_for(path)
        if _is_event_stream(upstream.headers):
            return _validate_stream_head(upstream, deadline, grammar)
        return _validate_body(upstream, path, grammar)
    if upstream.status == 429:
        return "rate_limit", _retry_after_seconds(upstream.headers, _COOLDOWN_ON_429_DEFAULT)
    if upstream.status in _AUTH_STATUS:
        return "auth", _COOLDOWN_ON_AUTH
    if 400 <= upstream.status < 500 and upstream.status not in _RETRYABLE_4XX:
        return None
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
        self.inflight = _InFlight()


def _wait_upstream(deadline: float) -> None:
    while _now() < deadline:
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
            handler: logging.Handler = RotatingFileHandler(
                ROUTER_LOG, encoding="utf-8", maxBytes=5_000_000, backupCount=3)
        except OSError:
            handler = logging.StreamHandler(sys.stderr)
    else:
        handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.handlers = [handler]


def run_forever() -> int:
    _configure_logging()
    _LOG.info("router starting on %s:%d, forwarding to %s:%d", ROUTER_HOST, ROUTER_PORT, PROXY_HOST, PROXY_PORT)
    _wait_upstream(_now() + ROUTER_START_TIMEOUT)
    server = _RouterServer((ROUTER_HOST, ROUTER_PORT))
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        _LOG.info("router stopping (KeyboardInterrupt)")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(run_forever())
