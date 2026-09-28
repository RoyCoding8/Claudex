"""Threaded local pool router for CLIProxyAPI."""
from __future__ import annotations

import gzip
import hmac
import json
import logging
import random
import re
import socket
import socketserver
import sys
import threading
import time
import uuid
import zlib
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
    CONFIG_ERRORS,
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
    ROUTER_DIRECT_ATTEMPTS,
    ROUTER_HOST,
    ROUTER_IDENTITY,
    ROUTER_LOG,
    ROUTER_POOL_PASSES,
    ROUTER_POOL_TIMEOUT,
    ROUTER_PORT,
    ROUTER_START_TIMEOUT,
)
from .models import _MAX_MODEL_ID_LENGTH, _MAX_MODELS_BYTES
from .pools import _is_structurally_valid, _reject_duplicate_json_keys, parse_pool_document
from .urls import http_url, is_local_host

_UPSTREAM_TIMEOUT = 600.0
_UPSTREAM_HEADER_TIMEOUT = 60.0
_POOL_REQUEST_TIMEOUT = ROUTER_POOL_TIMEOUT
_POOL_PASSES = ROUTER_POOL_PASSES
_DIRECT_ATTEMPTS = ROUTER_DIRECT_ATTEMPTS
_MODELS_CACHE_TTL = 30.0
_POOLS_STAT_INTERVAL = 0.25
_HEAD_PEEK_BYTES = 256 * 1024
_MAX_SSE_FRAME_BYTES = 256 * 1024
_MAX_BODY_BYTES = 128 * 1024 * 1024
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_MODELS_RESPONSE_BYTES = _MAX_MODELS_BYTES
_MAX_POOL_FILE_BYTES = 1 * 1024 * 1024
_SWEEP_BACKOFF = 1.0
_DIRECT_BACKOFF = 0.5
_HANDLER_TIMEOUT = 30
_LOG_MAX_BYTES = 5_000_000
_now = time.monotonic
_COOLDOWN_ON_429_DEFAULT = ROUTER_COOLDOWN_429
_COOLDOWN_ON_5XX = ROUTER_COOLDOWN_5XX
_COOLDOWN_ON_NETERR = ROUTER_COOLDOWN_NETWORK
_COOLDOWN_ON_AUTH = ROUTER_COOLDOWN_AUTH
_COOLDOWN_ON_PACED_429 = ROUTER_COOLDOWN_PACED_429
_COOLDOWN_ON_EMPTY = ROUTER_COOLDOWN_EMPTY
_AUTH_STATUS = frozenset({401, 403})
_CLIENT_DISCONNECT_ERRORS = (TimeoutError, BrokenPipeError, ConnectionResetError, ConnectionAbortedError)
_POOLED_PATHS = frozenset({"/v1/messages", "/v1/messages/count_tokens", "/v1/responses", "/v1/chat/completions"})
_INTEGRITY_FAILURES = frozenset({"empty", "malformed", "truncated", "oversized", "stream_error"})
_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "content-length",
})
_STRIPPED = frozenset({"authorization", "x-api-key", "host", "accept-encoding", "expect"})
_LOG = logging.getLogger("cx.router")
_SSE_BOUNDARY = re.compile(br"(?:\r\n|\r|\n){2}")
_HEADER_FIELD_NAME = re.compile(br"[!#$%&'*+\-.^_`|~0-9A-Za-z]+\Z")
_FORBIDDEN_FIELD_BYTES = frozenset(chr(code) for code in range(0x20) if code != 0x09) | {chr(0x7F)}


def _log_text(value: object) -> str:
    text = str(value)
    for secret in (ROUTER_API_KEY, PROXY_API_KEY):
        if secret:
            text = text.replace(secret, "<redacted>")
    return "".join(char if char.isprintable() else f"\\x{ord(char):02x}" for char in text)


def _parse_decimal(value: str) -> int | None:
    if re.fullmatch(r"[0-9]+", value) is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _parse_chunk_size(value: bytes) -> int | None:
    if re.fullmatch(br"[0-9A-Fa-f]+", value) is None:
        return None
    try:
        return int(value, 16)
    except ValueError:
        return None


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON constant: {value}")


def _valid_model_id(value: object) -> bool:
    return isinstance(value, str) and len(value) <= _MAX_MODEL_ID_LENGTH and value.isprintable() and bool(value.strip())


def _status_phrase(status: int, fallback: str) -> str:
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return fallback or ""


def _request_target(target: str) -> str | None:
    if not target.isascii():
        return None
    try:
        parsed = urlsplit(target)
        if parsed.fragment or not parsed.path.startswith("/"):
            return None
        if parsed.scheme or parsed.netloc:
            if (
                parsed.scheme != "http"
                or not parsed.netloc
                or parsed.username is not None
                or parsed.password is not None
            ):
                return None
            if not is_local_host(parsed.hostname or ""):
                return None
            port = parsed.port
            if port is not None and not 1 <= port <= 65535:
                return None
        elif not target.startswith("/") or target.startswith("//"):
            return None
        normalized = f"{parsed.path}?{parsed.query}" if parsed.query else parsed.path
        return None if normalized.startswith("//") else normalized
    except (UnicodeError, ValueError):
        return None


def _request_path(target: str) -> str | None:
    normalized = _request_target(target)
    return urlsplit(normalized).path if normalized is not None else None


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
    def __init__(self, path: Path) -> None:
        self._path, self._lock = path, threading.Lock()
        self._pools: dict[str, _Pool] = {}
        self._stat_identity: tuple[int, int, int, int] | None = None
        self._last_stat = 0.0

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
            stat = self._path.stat()
            identity = (stat.st_mtime_ns, stat.st_size, stat.st_ino, stat.st_ctime_ns)
        except FileNotFoundError:
            with self._lock:
                self._pools, self._stat_identity = {}, None
            return
        except OSError:
            return
        if identity == self._stat_identity:
            return
        with self._lock:
            if identity == self._stat_identity:
                return
            if stat.st_size > _MAX_POOL_FILE_BYTES:
                self._reject_oversized(identity)
                return
            try:
                with self._path.open("rb") as stream:
                    data = stream.read(_MAX_POOL_FILE_BYTES + 1)
            except OSError as error:
                _LOG.warning("pools.json unreadable; retaining prior state: %s", _log_text(error))
                return
            if len(data) > _MAX_POOL_FILE_BYTES:
                self._reject_oversized(identity)
                return
            try:
                payload = json.loads(data.decode("utf-8"), object_pairs_hook=_reject_duplicate_json_keys)
            except (ValueError, RecursionError) as error:
                _LOG.warning("pools.json unreadable; retaining prior state: %s", _log_text(error))
                self._stat_identity = identity
                return
            if not _is_structurally_valid(payload):
                _LOG.warning("pools.json is not a supported pool document; retaining prior state")
                self._stat_identity = identity
                return
            self._pools, self._stat_identity = _parse_pools(payload), identity
            _LOG.info("loaded %d enabled pool(s) from %s", len(self._pools), self._path)

    def _reject_oversized(self, identity: tuple[int, int, int, int]) -> None:
        _LOG.warning("pools.json exceeds the %d-byte limit; retaining prior state", _MAX_POOL_FILE_BYTES)
        self._stat_identity = identity


def _parse_pools(payload: Any) -> dict[str, _Pool]:
    pools, warnings = parse_pool_document(payload)
    for warning in warnings:
        _LOG.warning("pools.json: %s", _log_text(warning))
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

    def reserve(self, model: str, limit: int | None) -> bool:
        if limit is None:
            return True
        if limit <= 0:
            return False
        now = _now()
        with self._lock:
            hits = self._hits.setdefault(model, deque())
            self._prune(hits, now)
            if len(hits) >= limit:
                return False
            hits.append(now)
            return True


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
        _LOG.info("cooldown %.0fs on %s (%s)", seconds, _log_text(model), _log_text(reason))

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
        self._state: dict[str, tuple[int, int]] = {}
        self._lock = threading.Lock()

    def reserve(self, pool: str, size: int,
                choose: Callable[[int], int | None]) -> int | None:
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
    if pool.strategy == "round-robin":
        return _rotate_member(pool, cooldowns, exclude, limiter, start or 0, rotation)
    candidates = [member for member in pool.members if member.model not in exclude]
    if not candidates:
        return None
    selected = [member for member in candidates if cooldowns.is_ready(member.model)]
    if not selected:
        selected = candidates
    selector = _SELECTORS.get(pool.strategy, _SELECTORS["fill-first"])
    while selected:
        member = selector(selected, inflight)
        if limiter is None or limiter.reserve(member.model, member.limit):
            return member
        selected = [candidate for candidate in selected if candidate.model != member.model]
    return None


def _rotate_member(pool: _Pool, cooldowns: _CooldownTable, exclude: set[str],
                   limiter: _RateLimiter | None, start: int,
                   rotation: _Rotation | None) -> _Member | None:
    def scan(from_index: int) -> int | None:
        size = len(pool.members)
        order = [pool.members[(from_index + offset) % size] for offset in range(size)]
        order = [member for member in order if member.model not in exclude]
        tiers: tuple[Callable[[_Member], bool], ...]
        if limiter is None:
            tiers = (lambda m: cooldowns.is_ready(m.model), lambda m: True)
        else:
            tiers = (
                lambda m: cooldowns.is_ready(m.model) and limiter.reserve(m.model, m.limit),
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

def _forward_to_upstream(method: str, path: str, request_headers: dict[str, str], body: bytes,
                         deadline: float | None = None) -> _UpstreamResponse:
    leash = _UPSTREAM_TIMEOUT if deadline is None else max(1.0, deadline - _now())
    conn = HTTPConnection(PROXY_HOST, PROXY_PORT, timeout=min(_UPSTREAM_TIMEOUT, leash))
    try:
        conn.request(method, path, body=body, headers=request_headers)
        if conn.sock is not None:
            conn.sock.settimeout(min(_UPSTREAM_HEADER_TIMEOUT, leash))
        response = conn.getresponse()
    except BaseException:
        conn.close()
        raise
    body_socket = conn.sock or getattr(getattr(response.fp, "raw", None), "_sock", None)
    if body_socket is not None:
        body_socket.settimeout(min(_UPSTREAM_HEADER_TIMEOUT, leash))

    response_headers = [(key, value) for key, value in response.getheaders() if key.lower() not in _HOP_BY_HOP]
    gzip_response = (
        200 <= response.status < 300
        and any(key.lower() == "content-encoding" and value.strip().casefold() == "gzip"
                for key, value in response_headers)
    )
    body_source: Any = gzip.GzipFile(fileobj=response) if gzip_response else response
    if gzip_response:
        response_headers = [
            (key, value)
            for key, value in response_headers
            if key.lower() != "content-encoding" and not _strong_entity_validator(key, value)
        ]

    def iterate() -> Iterable[bytes]:
        try:
            while True:
                chunk = body_source.read1(65536)
                if not chunk:
                    remaining = getattr(response, "length", None)
                    if remaining not in (None, 0):
                        raise HTTPException("upstream closed before declared Content-Length")
                    return
                yield chunk
        finally:
            if body_source is not response:
                body_source.close()
            try:
                response.close()
            except OSError:
                pass

    return _UpstreamResponse(response.status, response.reason or "", response_headers, iterate(), conn, body_socket)


class _ModelListCache:
    def __init__(self) -> None:
        self._payload: dict[str, Any] | None = None
        self._checked_at: float | None = None
        self._lock = threading.Lock()

    def get(self) -> dict[str, Any]:
        now = _now()
        if self._checked_at is not None and now - self._checked_at < _MODELS_CACHE_TTL:
            return self._payload or {"object": "list", "data": []}
        with self._lock:
            now = _now()
            if self._checked_at is not None and now - self._checked_at < _MODELS_CACHE_TTL:
                return self._payload or {"object": "list", "data": []}
            payload = self._fetch()
            self._checked_at = _now()
            if payload is not None:
                self._payload = payload
            return self._payload or {"object": "list", "data": []}

    def _fetch(self) -> dict[str, Any] | None:
        conn = HTTPConnection(PROXY_HOST, PROXY_PORT, timeout=5.0)
        try:
            conn.request("GET", "/v1/models", headers={"Authorization": f"Bearer {PROXY_API_KEY}"})
            response = conn.getresponse()
            data = response.read(_MAX_MODELS_RESPONSE_BYTES + 1)
            if response.status != 200:
                _LOG.warning("upstream /v1/models returned %s", response.status)
                return None
            if len(data) > _MAX_MODELS_RESPONSE_BYTES:
                return None
            payload = json.loads(data.decode("utf-8"), parse_constant=_reject_json_constant)
            if (
                not isinstance(payload, dict)
                or payload.get("object") != "list"
                or not isinstance(payload.get("data"), list)
                or any(
                    not isinstance(model, dict)
                    or not _valid_model_id(model.get("id"))
                    for model in payload["data"]
                )
            ):
                raise ValueError("invalid models payload")
            return payload
        except (OSError, HTTPException, ValueError, RecursionError) as error:
            _LOG.warning("upstream /v1/models fetch failed: %s", _log_text(error))
            return None
        finally:
            try:
                conn.close()
            except OSError:
                pass


class _HeaderLineReader:
    def __init__(self, source: Any, lines: list[bytes]) -> None:
        self.source, self.lines = source, lines

    def readline(self, limit: int = -1) -> bytes:
        line = self.source.readline(limit)
        self.lines.append(line)
        return line

    def framing_headers(self) -> tuple[list[str], list[str], str | None]:
        content_lengths: list[str] = []
        transfer_encodings: list[str] = []
        invalid: str | None = None
        for line in self.lines:
            name, separator, value = line.partition(b":")
            normalized = name.strip().lower()
            if not separator or normalized not in {b"content-length", b"transfer-encoding"}:
                continue
            if _HEADER_FIELD_NAME.fullmatch(name) is None:
                invalid = invalid or normalized.decode("ascii")
                continue
            if value.startswith((b" ", b"\t")):
                value = value[1:]
            value = value.rstrip(b"\r\n")
            if normalized == b"content-length":
                content_lengths.append(value.decode("latin-1"))
            else:
                transfer_encodings.append(value.decode("latin-1"))
        return content_lengths, transfer_encodings, invalid

    def __getattr__(self, name: str) -> Any:
        return getattr(self.source, name)


class _RouterHandler(BaseHTTPRequestHandler):
    server_version, protocol_version, sys_version = ROUTER_IDENTITY, "HTTP/1.1", ""
    timeout = _HANDLER_TIMEOUT
    server: _RouterServer
    rfile: Any

    def log_message(self, format: str, *args: Any) -> None:
        _LOG.debug("%s - %s", _log_text(self.address_string()), _log_text(format % args))

    def log_error(self, format: str, *args: Any) -> None:
        _LOG.info("%s - %s", _log_text(self.address_string()), _log_text(format % args))

    def handle_one_request(self) -> None:
        self._body_read = False
        try:
            super().handle_one_request()
        except _CLIENT_DISCONNECT_ERRORS:
            self.close_connection = True
        finally:
            if not self._body_read:
                self._discard_body()

    def _discard_body(self) -> None:
        """Consume a request body nobody read, so the close is a clean one.

        Closing a socket that still holds unread data resets the connection, so a
        client handed that reset never reads the response already written to it.
        Clients send headers and body as two writes, so on a rejected request the
        body is routinely still unread when the reply goes out.
        """
        content_lengths, transfer_encodings, _ = getattr(self, "_raw_framing_headers", ([], [], None))
        if transfer_encodings:
            remaining: int | None = _MAX_BODY_BYTES
        elif len(content_lengths) == 1:
            size = _parse_decimal(content_lengths[0])
            remaining = None if size is None else min(size, _MAX_BODY_BYTES)
        else:
            return
        if remaining is None:
            return
        while remaining > 0:
            try:
                chunk = self.rfile.read(min(65536, remaining))
            except OSError:
                return
            if not chunk:
                return
            remaining -= len(chunk)

    def parse_request(self) -> bool:
        source = self.rfile
        lines: list[bytes] = []
        reader = _HeaderLineReader(source, lines)
        self.rfile = reader
        try:
            return super().parse_request()
        finally:
            self.rfile = source
            raw_words = getattr(self, "raw_requestline", b"").decode("iso-8859-1").rstrip("\r\n").split()
            if len(raw_words) >= 2:
                self.path = raw_words[1]
            self._raw_framing_headers = reader.framing_headers()

    def do_GET(self) -> None:
        path = _request_path(self.path)
        if path is None:
            self._send_json(400, {"error": {"message": "invalid request target"}})
        elif path in {"/", "/health", "/-/ready", "/-/health"}:
            self._send_json(200, {"status": "ok"})
        elif path == "/v1/models":
            if self._require_auth():
                self._handle_models()
        else:
            self._send_json(404, {"error": {"message": f"unknown path: {path}"}})

    def do_POST(self) -> None:
        target = _request_target(self.path)
        if target is None:
            self._send_json(400, {"error": {"message": "invalid request target"}})
            return
        path = urlsplit(target).path
        if path in _POOLED_PATHS:
            if self._require_auth():
                self._handle_pooled(target)
        elif path == "/v1/completions":
            if self._require_auth():
                self._handle_passthrough(target)
        else:
            self._send_json(404, {"error": {"message": f"unknown path: {path}"}})

    def _require_auth(self) -> bool:
        key = ROUTER_API_KEY.encode("utf-8")
        header = (self.headers.get("Authorization") or "").strip()
        api_key = self.headers.get("x-api-key", "").strip()
        parts = header.split(None, 1)
        bearer = (len(parts) == 2 and parts[0].casefold() == "bearer"
                  and bool(parts[1]) and not any(char.isspace() for char in parts[1])
                  and hmac.compare_digest(parts[1].encode("utf-8"), key))
        if bearer or hmac.compare_digest(api_key.encode("utf-8"), key):
            return True
        self._send_json(401, {"error": {"message": "invalid api key"}})
        return False

    def _handle_models(self) -> None:
        cache: _ModelListCache = self.server.models_cache
        registry: _PoolRegistry = self.server.pools
        payload = dict(cache.get())
        data = [
            model
            for model in payload.get("data", [])
            if isinstance(model, dict) and _valid_model_id(model.get("id"))
        ]
        upstream_ids = {str(model["id"]).strip() for model in data}
        for name in registry.names():
            if not _valid_model_id(name):
                continue
            if name in upstream_ids:
                _LOG.warning("pool %r shadows an upstream model with the same ID; "
                             "requests for it are served by the pool, not the model", _log_text(name))
            else:
                data.append({"id": name, "object": "model", "created": int(time.time()), "owned_by": "pool"})
        payload["object"], payload["data"] = "list", data
        body = json.dumps(payload).encode("utf-8")
        if len(body) > _MAX_MODELS_RESPONSE_BYTES:
            self._send_json(502, {"error": {"message": "router: model response too large"}})
            return
        self._send_bytes(200, body)

    def _handle_pooled(self, target: str) -> None:
        body = self._read_body()
        if body is None:
            return
        try:
            payload = json.loads(body.decode("utf-8"), parse_constant=_reject_json_constant) if body else {}
        except (ValueError, RecursionError) as error:
            self._send_json(400, {"error": {"message": f"invalid JSON body: {error}"}})
            return
        if not isinstance(payload, dict):
            self._send_json(400, {"error": {"message": "body must be a JSON object"}})
            return
        model = payload.get("model")
        if model is None or (isinstance(model, str) and not model.strip()):
            self._send_json(400, {"error": {"message": "missing 'model' field"}})
            return
        if not isinstance(model, str):
            self._send_json(400, {"error": {"message": "'model' must be a string"}})
            return
        requested_model = model.strip()
        registry: _PoolRegistry = self.server.pools
        pool = registry.get(requested_model)
        if pool is None:
            self._forward_once(target, dict(self.headers), body)
        else:
            self._forward_pool(pool, target, payload)

    def _forward_pool(self, pool: _Pool, path: str, payload: dict[str, Any]) -> None:
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
            member = _pick_member(pool, cooldowns, tried, limiter, start, inflight,
                                  rotation=rotation if not tried else None)
            if member is None:
                break
            attempt += 1
            tried.add(member.model)
            rewritten = _rewrite_model(payload, member.model)
            _LOG.info("request=%s attempt=%d pool=%s member=%s", request_id, attempt,
                      _log_text(pool.name), _log_text(member.model))
            with inflight.hold(member.model):
                upstream, streamed = None, False
                try:
                    upstream = _forward_to_upstream(
                        "POST", path, _upstream_headers(self.headers, rewritten), rewritten, deadline)
                    verdict = _judge_response(upstream, path=urlsplit(path).path, deadline=deadline)
                    if verdict is None:
                        streamed = True
                        if self._stream_upstream(upstream, request_id=request_id, member=member.model, deadline=deadline, path=urlsplit(path).path):
                            cooldowns.clear(member.model)
                        else:
                            cooldowns.cooldown(member.model, _COOLDOWN_ON_NETERR, "stream_drop")
                        return
                    category, delay = verdict
                    if category == "rate_limit" and not _has_retry_after(upstream.headers):
                        delay = member.cooldown if member.cooldown is not None else (
                            _COOLDOWN_ON_PACED_429 if member.limit is not None else delay
                        )
                    cooldowns.cooldown(member.model, delay, category)
                    failures.append({"category": category, "status": upstream.status})
                    _LOG.warning(
                        "request=%s pool=%s member=%s status=%d category=%s cooldown=%.1fs",
                        request_id, _log_text(pool.name), _log_text(member.model), upstream.status,
                        category, delay,
                    )
                except (OSError, HTTPException) as error:
                    if streamed:
                        _LOG.warning("request=%s member=%s client lost after commit: %s", request_id,
                                     _log_text(member.model), _log_text(error))
                        return
                    cooldowns.cooldown(member.model, _COOLDOWN_ON_NETERR, "network")
                    failures.append({"category": "network", "status": None})
                    _LOG.warning("request=%s pool=%s member=%s category=network error=%s", request_id,
                                 _log_text(pool.name), _log_text(member.model), _log_text(error))
                    continue
                finally:
                    if upstream is not None and not streamed:
                        upstream.close()
        timed_out = _now() >= deadline
        _LOG.warning("request=%s pool=%s exhausted attempts=%d sweeps=%d timed_out=%s", request_id,
                     _log_text(pool.name), attempt, sweep, timed_out)
        self._send_json(503, {"error": {
            "message": "router: no pool member succeeded", "type": "pool_exhausted",
            "request_id": request_id, "attempts": failures,
        }})

    def _handle_passthrough(self, target: str) -> None:
        body = self._read_body()
        if body is not None:
            self._forward_once(target, dict(self.headers), body)

    def _forward_once(self, path: str, headers: dict[str, str], body: bytes) -> None:
        route_path = urlsplit(path).path
        pooled = route_path in _POOLED_PATHS
        attempts = _DIRECT_ATTEMPTS if pooled else 1
        request_id = uuid.uuid4().hex[:12]
        deadline = _now() + _POOL_REQUEST_TIMEOUT
        failures: list[dict[str, Any]] = []
        for attempt in range(1, attempts + 1):
            if attempt > 1:
                remaining = deadline - _now()
                if remaining <= 0:
                    break
                if pause := min(_DIRECT_BACKOFF * (attempt - 1), remaining):
                    time.sleep(pause)
                _LOG.info("request=%s direct retry attempt=%d path=%s", request_id, attempt, _log_text(path))
            upstream = None
            try:
                upstream = _forward_to_upstream("POST", path, _upstream_headers(headers, body), body, deadline)
            except (OSError, HTTPException) as error:
                failures.append({"category": "network", "status": None})
                _LOG.warning("request=%s direct attempt=%d path=%s category=network error=%s",
                             request_id, attempt, _log_text(path), _log_text(error))
                continue
            # A non-pooled path has no other member to try, so there is nothing to
            # gain from judging the body: the upstream's answer is the answer, and
            # re-reading a broken stream would only report what the client already
            # saw. A pooled path is judged so a bad member can be left behind.
            if not pooled:
                self._stream_upstream(
                    upstream, request_id=request_id,
                    member=None,
                    deadline=deadline, path=route_path)
                return
            verdict = _judge_response(upstream, path=route_path, deadline=deadline)
            if verdict is None or verdict[0] not in _INTEGRITY_FAILURES:
                self._stream_upstream(
                    upstream, request_id=request_id,
                    member=None,
                    deadline=deadline, path=route_path)
                return
            upstream.close()
            failures.append({"category": verdict[0], "status": upstream.status})
            _LOG.warning("request=%s direct attempt=%d path=%s status=%d category=%s",
                         request_id, attempt, _log_text(path), upstream.status, verdict[0])
        category = failures[-1]["category"] if failures else "network"
        _LOG.warning("request=%s direct path=%s exhausted attempts=%d category=%s",
                     request_id, _log_text(path), len(failures), category)
        message = "router: upstream unreachable" if category == "network" else "router: upstream response interrupted"
        self._send_json(502, {"error": {
            "message": message, "type": category,
            "request_id": request_id, "attempts": failures,
        }})

    def _read_body(self) -> bytes | None:
        self._body_read = True
        content_lengths, transfer_encodings, invalid = getattr(self, "_raw_framing_headers", ([], [], None))
        if invalid == "content-length":
            self._send_json(400, {"error": {"message": "invalid Content-Length"}})
            return None
        if invalid == "transfer-encoding":
            self._send_json(400, {"error": {"message": "invalid Transfer-Encoding"}})
            return None
        if transfer_encodings and content_lengths:
            self._send_json(400, {"error": {"message": "conflicting request framing"}})
            return None
        if transfer_encodings:
            if len(transfer_encodings) != 1 or transfer_encodings[0].strip().lower() != "chunked":
                self._send_json(400, {"error": {"message": "invalid Transfer-Encoding"}})
                return None
            return self._read_chunked_body()
        if len(content_lengths) > 1:
            self._send_json(400, {"error": {"message": "invalid Content-Length"}})
            return None
        length = content_lengths[0] if content_lengths else None
        if length is None:
            return b""
        size = _parse_decimal(length)
        if size is None:
            self._send_json(400, {"error": {"message": "invalid Content-Length"}})
            return None
        if size > _MAX_BODY_BYTES:
            self._send_json(413, {"error": {"message": "request too large"}})
            return None
        body = self.rfile.read(size) if size else b""
        if len(body) != size:
            self._send_json(400, {"error": {"message": "short Content-Length body"}})
            return None
        return body

    def _read_chunked_body(self) -> bytes | None:
        chunks, total = [], 0
        while True:
            line = self.rfile.readline(65536)
            if not line.endswith(b"\r\n"):
                self._send_json(400, {"error": {"message": "invalid chunked framing"}})
                return None
            token = line[:-2].split(b";", 1)[0]
            size = _parse_chunk_size(token)
            if size is None:
                self._send_json(400, {"error": {"message": "invalid chunked framing"}})
                return None
            if size == 0:
                break
            total += size
            if total > _MAX_BODY_BYTES:
                self._send_json(413, {"error": {"message": "request too large"}})
                return None
            chunk = self.rfile.read(size)
            if len(chunk) != size or self.rfile.read(2) != b"\r\n":
                self._send_json(400, {"error": {"message": "invalid chunked framing"}})
                return None
            chunks.append(chunk)
        trailer_total = 0
        while trailer := self.rfile.readline(65536):
            trailer_total += len(trailer)
            if trailer_total > _MAX_BODY_BYTES:
                self._send_json(413, {"error": {"message": "request too large"}})
                return None
            if trailer == b"\r\n":
                return b"".join(chunks)
            if not trailer.endswith(b"\r\n"):
                self._send_json(400, {"error": {"message": "invalid chunked framing"}})
                return None
        self._send_json(400, {"error": {"message": "invalid chunked framing"}})
        return None

    def _send_bytes(self, status: int, body: bytes) -> None:
        # The header alone does not stop the handler: BaseHTTPRequestHandler reads
        # close_connection, not the header it wrote, and so would block on the next
        # request line of a socket the peer is about to close.
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except _CLIENT_DISCONNECT_ERRORS:
            pass

    def _send_json(self, status: int, payload: Any) -> None:
        self._send_bytes(status, json.dumps(payload).encode("utf-8"))

    def _write_sse_error(self) -> None:
        try:
            self.wfile.write(b'event: error\ndata: {"type":"error","error":{"message":"upstream stream interrupted"}}\n\n')
            self.wfile.flush()
        except _CLIENT_DISCONNECT_ERRORS:
            pass

    def _stream_upstream(self, upstream: _UpstreamResponse, *, request_id: str | None = None,
                         member: str | None = None, deadline: float | None = None,
                         path: str = "/v1/messages") -> bool:
        upstream.relax_timeout(deadline)
        streaming = _is_event_stream(upstream.headers)
        grammar = _grammar_for(path)
        try:
            if not streaming:
                body: bytes | None
                if upstream.buffered is not None:
                    body = upstream.buffered
                else:
                    try:
                        body = _read_bounded_body(upstream)
                    except (OSError, HTTPException, EOFError, zlib.error) as error:
                        _LOG.warning("request=%s member=%s upstream body failed before response: %s", request_id,
                                     _log_text(member), _log_text(error))
                        self._send_json(502, {"error": {"message": "router: upstream response interrupted"}})
                        return False
                if body is None:
                    _LOG.warning("request=%s member=%s upstream response exceeded cap", request_id, _log_text(member))
                    self._send_json(502, {"error": {"message": "router: upstream response too large"}})
                    return False
                original_body = body
                if member and not 400 <= upstream.status < 600:
                    body = _attest_route(body, member)
                if not self._send_upstream_headers(upstream, len(body), body != original_body):
                    self._send_json(502, {"error": {"message": "router: invalid upstream headers"}})
                    return False
                try:
                    self.wfile.write(body)
                    self.wfile.flush()
                except _CLIENT_DISCONNECT_ERRORS:
                    pass
                return True
            if not self._send_upstream_headers(upstream, None):
                self._send_json(502, {"error": {"message": "router: invalid upstream headers"}})
                return False
            sent, clean, has_content, terminal, saw_error = False, True, False, False, False
            sse_buffer = b""
            try:
                for chunk in upstream.body_iter:
                    try:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    except _CLIENT_DISCONNECT_ERRORS:
                        _LOG.info("request=%s member=%s client disconnected after_bytes=%s", request_id,
                                  _log_text(member), sent)
                        return True
                    sent = True
                    frames, sse_buffer = _split_sse_frames(sse_buffer + chunk)
                    for frame in frames:
                        event, payload = _sse_parts(frame)
                        if grammar.has_content(event, payload):
                            has_content = True
                        if _sse_frame_is_error(frame):
                            clean = False
                            saw_error = True
                            _LOG.warning("request=%s member=%s forwarded trailing SSE error", request_id, _log_text(member))
                        if grammar.is_terminal(event):
                            terminal = True
                    if len(sse_buffer) > _MAX_SSE_FRAME_BYTES:
                        _LOG.warning("request=%s member=%s SSE frame exceeded cap", request_id, _log_text(member))
                        self._write_sse_error()
                        return False
            except (OSError, HTTPException, EOFError, zlib.error) as error:
                _LOG.warning("request=%s member=%s upstream SSE interrupted after_bytes=%s: %s", request_id,
                             _log_text(member), sent, _log_text(error))
                self._write_sse_error()
                return False
            if has_content and not terminal and not saw_error:
                _LOG.warning("request=%s member=%s SSE ended without a terminal event", request_id, _log_text(member))
                self._write_sse_error()
                clean = False
            return clean
        finally:
            upstream.close()

    def _send_upstream_headers(self, upstream: _UpstreamResponse, content_length: int | None,
                               body_changed: bool = False) -> bool:
        if any(not _valid_upstream_header(key, value) for key, value in upstream.headers):
            return False
        reason = _status_phrase(upstream.status, upstream.reason)
        self.send_response(upstream.status, reason if _valid_field_value(reason) else "")
        for key, value in upstream.headers:
            lower = key.lower()
            if lower in _RESPONSE_IDENTITY_HEADERS or (body_changed and lower in _ENTITY_VALIDATORS):
                continue
            self.send_header(key, value)
        if content_length is not None:
            self.send_header("Content-Length", str(content_length))
        self.send_header("Connection", "close")
        self.end_headers()
        return True


_RESPONSE_IDENTITY_HEADERS = frozenset({"date", "server"})
_ENTITY_VALIDATORS = frozenset({"content-md5", "digest", "etag"})


def _valid_field_value(value: str) -> bool:
    return not _FORBIDDEN_FIELD_BYTES.intersection(value)


def _valid_upstream_header(key: str, value: str) -> bool:
    try:
        encoded = key.encode("ascii")
    except UnicodeEncodeError:
        return False
    return _HEADER_FIELD_NAME.fullmatch(encoded) is not None and _valid_field_value(value)


def _strong_entity_validator(key: str, value: str) -> bool:
    lower = key.lower()
    return lower in {"content-md5", "digest"} or (lower == "etag" and not value.lstrip().startswith("W/"))


def _rewrite_model(parsed: dict[str, Any], real_model: str) -> bytes:
    payload = dict(parsed)
    payload["model"] = real_model
    return json.dumps(payload).encode("utf-8")


def _attested_endpoint() -> str:
    return http_url(ROUTER_HOST, ROUTER_PORT, "/v1")


def _model_provider(model_id: object) -> str | None:
    if not isinstance(model_id, str) or not model_id:
        return None
    parts = model_id.split("/")
    if len(parts) == 2:
        return parts[0] or None
    if len(parts) >= 3:
        return parts[1] or None
    return None


def _attest_route(body: bytes, member_model: str) -> bytes:
    try:
        parsed = json.loads(body)
        if not isinstance(parsed, dict) or not parsed:
            return body
        if "model" not in parsed:
            return body
        payload = dict(parsed)
        provider = _model_provider(payload.get("model")) \
            or _model_provider(member_model)
        if payload.get("provider") in (None, "") and provider:
            payload["provider"] = provider
        if payload.get("tier") in (None, "") and member_model.endswith(":free"):
            payload["tier"] = "free"
        if payload.get("endpoint") in (None, ""):
            payload["endpoint"] = _attested_endpoint()
        return json.dumps(payload).encode("utf-8")
    except (ValueError, RecursionError):
        return body


def _upstream_headers(incoming: Any, body: bytes) -> dict[str, str]:
    output: dict[str, str] = {}
    seen: set[str] = set()
    for key in incoming.keys() if hasattr(incoming, "keys") else []:
        lower = key.lower()
        if lower in _HOP_BY_HOP or lower in _STRIPPED or lower in seen:
            continue
        value = incoming[key] if hasattr(incoming, "__getitem__") else incoming.get(key)
        if value is not None:
            output[key] = value
            seen.add(lower)
    if "content-type" not in seen:
        output["Content-Type"] = "application/json"
    output["Content-Length"] = str(len(body))
    output["Accept-Encoding"] = "identity"
    output["Host"] = http_url(PROXY_HOST, PROXY_PORT).removeprefix("http://")
    output["Authorization"] = f"Bearer {PROXY_API_KEY}"
    output["x-api-key"] = PROXY_API_KEY
    if "anthropic-version" not in seen:
        output["anthropic-version"] = "2023-06-01"
    if "accept" not in seen:
        output["Accept"] = "application/json, text/event-stream"
    return output


def _has_retry_after(headers: list[tuple[str, str]]) -> bool:
    return any(key.lower() == "retry-after" for key, _ in headers)


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


def _split_sse_frames(buffer: bytes) -> tuple[list[bytes], bytes]:
    frames = []
    while match := _SSE_BOUNDARY.search(buffer):
        frames.append(buffer[:match.start()])
        buffer = buffer[match.end():]
    return frames, buffer


def _sse_parts(frame: bytes) -> tuple[str, Any]:
    event, data_lines = "", []
    for line in frame.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    raw = "\n".join(data_lines).strip()
    if raw == "[DONE]":
        return "done", None
    try:
        payload = json.loads(raw) if raw else None
    except (ValueError, RecursionError):
        payload = None
    if not event and isinstance(payload, dict):
        event = str(payload.get("type") or "")
    return event, payload


def _sse_frame_is_error(frame: bytes) -> bool:
    return _sse_parts(frame)[0] in _ERROR_EVENTS


def _chat_delta_has_content(payload: Any) -> bool:
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
    terminal_events: frozenset[str] = frozenset()

    def has_content(self, event: str, payload: Any) -> bool:
        return event in self.content_events or bool(self.payload_probe and self.payload_probe(payload))

    def is_terminal(self, event: str) -> bool:
        return event in self.terminal_events


_STREAM_ERROR = ("stream_error", _COOLDOWN_ON_5XX)
_EMPTY = ("empty", _COOLDOWN_ON_EMPTY)

_ANTHROPIC_GRAMMAR = _Grammar(
    frozenset({"content_block_start", "content_block_delta"}),
    {"error": _STREAM_ERROR, "message_stop": _EMPTY},
    "content",
    terminal_events=frozenset({"message_stop"}),
)
_RESPONSES_GRAMMAR = _Grammar(
    frozenset({"response.output_item.added", "response.output_text.delta",
               "response.reasoning_summary_text.delta", "response.reasoning_text.delta",
               "response.function_call_arguments.delta"}),
    {"error": _STREAM_ERROR, "response.failed": _STREAM_ERROR,
     "response.incomplete": _EMPTY, "response.completed": _EMPTY},
    "output",
    terminal_events=frozenset({"response.completed"}),
)
_CHAT_GRAMMAR = _Grammar(frozenset(), {"error": _STREAM_ERROR}, "choices", _chat_delta_has_content, frozenset({"done"}))

_GRAMMARS = {
    "/v1/messages": _ANTHROPIC_GRAMMAR,
    "/v1/responses": _RESPONSES_GRAMMAR,
    "/v1/chat/completions": _CHAT_GRAMMAR,
}
_ERROR_EVENTS = frozenset({"error", "response.failed"})


def _grammar_for(path: str) -> _Grammar:
    route = urlsplit(path).path
    return _GRAMMARS.get(route.removesuffix("/count_tokens"), _ANTHROPIC_GRAMMAR)


def _validate_stream_head(upstream: _UpstreamResponse, deadline: float,
                          grammar: _Grammar) -> tuple[str, float] | None:
    iterator, chunks, buffered, total = iter(upstream.body_iter), [], b"", 0
    outcome: tuple[str, float] | None = None
    has_content = False
    try:
        while total < _HEAD_PEEK_BYTES and _now() < deadline:
            chunk = next(iterator)
            chunks.append(chunk)
            total += len(chunk)
            buffered += chunk
            frames, buffered = _split_sse_frames(buffered)
            for frame in frames:
                if len(frame) > _MAX_SSE_FRAME_BYTES:
                    outcome = "oversized", _COOLDOWN_ON_5XX
                    break
                event, payload = _sse_parts(frame)
                if grammar.has_content(event, payload):
                    has_content = True
                    break
                if event in grammar.verdicts:
                    outcome = grammar.verdicts[event]
                    break
            if outcome is not None or has_content:
                break
            if len(buffered) > _MAX_SSE_FRAME_BYTES:
                outcome = "oversized", _COOLDOWN_ON_5XX
                break
            if total >= _HEAD_PEEK_BYTES:
                outcome = "empty", _COOLDOWN_ON_EMPTY
                break
    except StopIteration:
        outcome = outcome or ("empty", _COOLDOWN_ON_EMPTY)
    except (OSError, HTTPException, EOFError, zlib.error):
        outcome = "truncated", _COOLDOWN_ON_NETERR
    if outcome is None and not has_content:
        outcome = "empty", _COOLDOWN_ON_EMPTY
    upstream.body_iter = chain(chunks, iterator)
    return outcome


def _validate_body(upstream: _UpstreamResponse, path: str,
                   grammar: _Grammar, deadline: float) -> tuple[str, float] | None:
    upstream.relax_timeout(deadline)
    try:
        body = _read_bounded_body(upstream)
    except (OSError, HTTPException, EOFError, zlib.error):
        return "truncated", _COOLDOWN_ON_NETERR
    if body is None:
        return "oversized", _COOLDOWN_ON_5XX
    upstream.body_iter, upstream.buffered = iter((body,)), body
    if not body.strip():
        return "empty", _COOLDOWN_ON_EMPTY
    try:
        payload = json.loads(body.decode("utf-8"), parse_constant=_reject_json_constant)
    except (ValueError, RecursionError):
        return "malformed", _COOLDOWN_ON_EMPTY
    if not isinstance(payload, dict):
        return "malformed", _COOLDOWN_ON_EMPTY
    if path.endswith("/count_tokens"):
        return None
    content = payload.get(grammar.body_field)
    if not isinstance(content, list) or not content:
        return "empty", _COOLDOWN_ON_EMPTY
    return None


def _judge_response(upstream: _UpstreamResponse, *, path: str, deadline: float) -> tuple[str, float] | None:
    """Name the failure and say how long the member is out, or None if it is sound.

    A member that fails is a member to leave: the next one may well serve the
    request, and stopping at the first refusal lets one member that rejects a
    capability take the whole pool down. The response only decides the label and
    the cooldown; whether to keep going is the caller's, and for a pool it always
    is. A 2xx the grammar accepts is the one answer that is not a failure, which
    is why this is not a failure count.
    """
    if 200 <= upstream.status < 300:
        grammar = _grammar_for(path)
        if _is_event_stream(upstream.headers):
            return _validate_stream_head(upstream, deadline, grammar)
        return _validate_body(upstream, path, grammar, deadline)
    if upstream.status == 429:
        return "rate_limit", _retry_after_seconds(upstream.headers, _COOLDOWN_ON_429_DEFAULT)
    if upstream.status in _AUTH_STATUS:
        return "auth", _COOLDOWN_ON_AUTH
    return "upstream", _COOLDOWN_ON_5XX


def _is_event_stream(headers: list[tuple[str, str]]) -> bool:
    return any(key.lower() == "content-type" and "text/event-stream" in value.lower() for key, value in headers)


def _read_bounded_body(upstream: _UpstreamResponse) -> bytes | None:
    if upstream.buffered is not None:
        return upstream.buffered if len(upstream.buffered) <= _MAX_RESPONSE_BYTES else None
    data = bytearray()
    for chunk in upstream.body_iter:
        data.extend(chunk)
        if len(data) > _MAX_RESPONSE_BYTES:
            return None
    return bytes(data)


class _RouterServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self) -> None:
        # HTTPServer.server_bind resolves the host name between bind() and
        # listen(). A reverse-DNS lookup that stalls there holds the port bound
        # but unserved, and the launcher's health check then fails for reasons
        # that name no resolver. The router never serves a name, so skip it.
        socketserver.TCPServer.server_bind(self)
        host = self.server_address[0]
        self.server_name = host.decode() if isinstance(host, (bytes, bytearray)) else host
        self.server_port = self.server_address[1]

    def __init__(self, address: tuple[str, int]) -> None:
        self.address_family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
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
    raise RuntimeError(
        f"CLIProxyAPI unreachable at {http_url(PROXY_HOST, PROXY_PORT)} — start it before the router.")


class _BoundedStreamHandler(logging.StreamHandler):
    def __init__(self, stream: Any, cap: int) -> None:
        super().__init__(stream)
        self._cap, self._written = cap, 0

    def emit(self, record: logging.LogRecord) -> None:
        if self._written >= self._cap:
            return
        self._written += len(self.format(record).encode("utf-8", "backslashreplace"))
        super().emit(record)


def _configure_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    handler: logging.Handler
    if ROUTER_LOG:
        try:
            ROUTER_LOG.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(ROUTER_LOG, encoding="utf-8", maxBytes=_LOG_MAX_BYTES, backupCount=3)
        except OSError:
            handler = _BoundedStreamHandler(sys.stderr, _LOG_MAX_BYTES)
    else:
        handler = _BoundedStreamHandler(sys.stderr, _LOG_MAX_BYTES)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.handlers = [handler]


def run_forever() -> int:
    _configure_logging()
    if CONFIG_ERRORS:
        for problem in CONFIG_ERRORS:
            _LOG.error("configuration error: %s", _log_text(problem))
        return 1
    _LOG.info("router starting on %s, forwarding to %s",
              http_url(ROUTER_HOST, ROUTER_PORT), http_url(PROXY_HOST, PROXY_PORT))
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
