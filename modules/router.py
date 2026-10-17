"""Threaded local pool router for CLIProxyAPI."""
from __future__ import annotations

import gzip
import hmac
import json
import logging
import math
import os
import random
import re
import secrets
import socket
import socketserver
import sys
import threading
import time
import uuid
import zlib
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC
from email.utils import parsedate_to_datetime
from http import HTTPStatus
from http.client import HTTPConnection, HTTPException, HTTPResponse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import chain
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, cast
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
    ROUTER_STATE_FILE,
)
from .models import _MAX_MODEL_ID_LENGTH, _MAX_MODELS_BYTES
from .pools import _is_structurally_valid, _reject_duplicate_json_keys, parse_pool_document
from .router_starter import clear_router_identity, publish_router_identity
from .router_state import RouterState
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
_MAX_BODY_BYTES = 16 * 1024 * 1024
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_ACTIVE_REQUESTS = 8
_MAX_MODELS_RESPONSE_BYTES = _MAX_MODELS_BYTES
_MAX_POOL_FILE_BYTES = 1 * 1024 * 1024
_SWEEP_BACKOFF = 1.0
_DIRECT_BACKOFF = 0.5
_HANDLER_TIMEOUT = 30
_CLIENT_REQUEST_TIMEOUT = 60.0
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


def _socket_timeout(deadline: float | None, maximum: float) -> float:
    if deadline is None:
        return maximum
    remaining = deadline - _now()
    if remaining <= 0:
        raise TimeoutError("upstream request deadline exceeded")
    return min(maximum, remaining)
_LOG = logging.getLogger("cx.router")
_SSE_BOUNDARY = re.compile(br"(?>\r\n|\r|\n){2}")
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


def _safe_log_path(target: str) -> str:
    try:
        path = urlsplit(target).path
    except ValueError:
        return "<invalid>"
    return path if path.startswith("/") else "<invalid>"


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


class _Scheduler:
    def __init__(self, state: RouterState) -> None:
        self.state = state
        self._counts: dict[str, int] = {}
        self._cursors: dict[str, tuple[tuple[str, ...], int]] = {}
        self._lock = threading.Lock()

    def count(self, model: str) -> int:
        with self._lock:
            return self._counts.get(model, 0)

    @contextmanager
    def hold(self, pool: _Pool, tried: set[str]) -> Iterator[_Member | None]:
        with self._lock:
            members = list(pool.members)
            identity = tuple(m.model for m in members)
            known, cursor = self._cursors.get(pool.name, (identity, 0))
            cursor = cursor if known == identity else 0
            if pool.strategy == "round-robin":
                members = members[cursor:] + members[:cursor]
            else:
                members.sort(key=lambda m: -math.log1p(-random.random()) / m.rpm)
                if pool.strategy == "least-busy":
                    members.sort(key=lambda m: self._counts.get(m.model, 0))
                elif pool.strategy == "fill-first":
                    members.sort(key=lambda m: m.priority)
            members = [m for m in members if m.model not in tried]
            members.sort(key=lambda m: not self.state.is_ready(m.model))
            member = next((m for m in members if self.state.reserve(m.model, m.limit)), None)
            if member is not None:
                self._counts[member.model] = self._counts.get(member.model, 0) + 1
                if pool.strategy == "round-robin" and not tried:
                    self._cursors[pool.name] = (identity, (pool.members.index(member) + 1) % len(identity))
        try:
            yield member
        finally:
            if member is not None:
                with self._lock:
                    remaining = self._counts[member.model] - 1
                    if remaining:
                        self._counts[member.model] = remaining
                    else:
                        del self._counts[member.model]


@dataclass(slots=True)
class _UpstreamResponse:
    status: int
    reason: str
    headers: list[tuple[str, str]]
    body_iter: Iterable[bytes]
    connection: HTTPConnection
    prefix: tuple[bytes, ...] = ()
    scanner: _SseScan | None = None
    buffered: bytes | None = None
    _closed: bool = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.connection.close()
        except OSError:
            pass

def _forward_to_upstream(method: str, path: str, request_headers: dict[str, str], body: bytes,
                         deadline: float) -> _UpstreamResponse:
    conn = HTTPConnection(PROXY_HOST, PROXY_PORT, timeout=_socket_timeout(deadline, _UPSTREAM_TIMEOUT))
    conn.response_class = _deadline_response_class(deadline)
    try:
        conn.connect()
        if conn.sock is not None:
            conn.sock.settimeout(_socket_timeout(deadline, _UPSTREAM_TIMEOUT))
        conn.request(method, path, body=body, headers=request_headers)
        response = conn.getresponse()
    except BaseException:
        conn.close()
        raise
    if isinstance(response.fp, _DeadlineReader):
        response.fp.max_timeout = _UPSTREAM_TIMEOUT

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
                    remaining_length = getattr(response, "length", None)
                    if remaining_length not in (None, 0):
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

    return _UpstreamResponse(response.status, response.reason or "", response_headers, iterate(), conn)


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


class _DeadlineReader:
    def __init__(self, source: Any, connection: Any, deadline: float,
                 max_timeout: float = _HANDLER_TIMEOUT,
                 timeout_message: str = "client request deadline exceeded") -> None:
        self.source = source
        self.connection = connection
        self.deadline = deadline
        self.max_timeout = max_timeout
        self.timeout_message = timeout_message
        self.buffer = bytearray()
        self.timed_out = False

    def _fill(self, size: int) -> bool:
        remaining = self.deadline - _now()
        if remaining <= 0:
            self.timed_out = True
            raise TimeoutError(self.timeout_message)
        self.connection.settimeout(min(self.max_timeout, remaining))
        try:
            chunk = self.source.read1(size)
        except TimeoutError:
            self.timed_out = True
            raise
        if not chunk:
            return False
        self.buffer.extend(chunk)
        return True

    def read1(self, size: int = -1) -> bytes:
        if size < 0:
            size = 65536
        if not self.buffer and not self._fill(size):
            return b""
        result = bytes(self.buffer[:size])
        del self.buffer[:size]
        return result

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            raise ValueError("request reads must have a size")
        while len(self.buffer) < size:
            if not self._fill(min(65536, size - len(self.buffer))):
                break
        result = bytes(self.buffer[:size])
        del self.buffer[:size]
        return result

    def readline(self, size: int = -1) -> bytes:
        while True:
            newline = self.buffer.find(b"\n")
            if newline >= 0 and (size < 0 or newline + 1 <= size):
                length = newline + 1
                break
            if size >= 0 and len(self.buffer) >= size:
                length = size
                break
            read_size = 4096 if size < 0 else min(4096, size - len(self.buffer))
            if not self._fill(max(1, read_size)):
                length = len(self.buffer)
                break
        result = bytes(self.buffer[:length])
        del self.buffer[:length]
        return result

    def close(self) -> None:
        self.source.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.source, name)


def _deadline_response_class(deadline: float) -> type[HTTPResponse]:
    class _DeadlineHTTPResponse(HTTPResponse):
        def __init__(self, sock: Any, *args: Any, **kwargs: Any) -> None:
            super().__init__(sock, *args, **kwargs)
            self.fp = cast(
                Any,
                _DeadlineReader(
                    self.fp, sock, deadline, _UPSTREAM_HEADER_TIMEOUT, "upstream request deadline exceeded"
                ),
            )

    return _DeadlineHTTPResponse


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

    def setup(self) -> None:
        super().setup()
        self.rfile = _DeadlineReader(self.rfile, self.connection, _now() + _CLIENT_REQUEST_TIMEOUT)

    def log_message(self, format: str, *args: Any) -> None:
        _LOG.debug("%s - %s", _log_text(self.client_address[0]), _log_text(format % args))

    def log_error(self, format: str, *args: Any) -> None:
        _LOG.info("%s - %s", _log_text(self.client_address[0]), _log_text(format % args))

    def log_request(self, code: str | int = "-", size: str | int = "-") -> None:
        _LOG.debug("%s - %s %s %s %s", _log_text(self.client_address[0]), _log_text(self.command),
                   _log_text(_safe_log_path(self.path)), code, size)

    def handle_one_request(self) -> None:
        self._body_read = False
        try:
            super().handle_one_request()
        except TimeoutError:
            self._client_read_timed_out()
        except _CLIENT_DISCONNECT_ERRORS:
            self.close_connection = True
        else:
            if getattr(self.rfile, "timed_out", False):
                self._client_read_timed_out()
        finally:
            if not self._body_read:
                self._discard_body()

    def _client_read_timed_out(self) -> None:
        body_was_started = self._body_read
        self._body_read = True
        self.close_connection = True
        if not body_was_started:
            self._send_json(408, {"error": {"message": "request timed out"}})

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
            self._send_json(200, {"status": "ok", "pid": os.getpid(), "token": self.server.instance_token})
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
        if path not in _POOLED_PATHS | {"/v1/completions", "/-/shutdown"}:
            self._send_json(404, {"error": {"message": f"unknown path: {path}"}})
            return
        if not self._require_auth():
            return
        if path == "/-/shutdown":
            token = self.headers.get("X-CX-Router-Token", "")
            if not hmac.compare_digest(token.encode("utf-8"), self.server.instance_token.encode("ascii")):
                self._send_json(409, {"error": {"message": "router instance changed"}})
                return
            self._send_json(202, {"status": "stopping"})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        body = self._read_body()
        if body is None:
            return
        if not self.server.dispatch_slots.acquire(blocking=False):
            self._send_json(503, {"error": {"message": "router: capacity exhausted"}})
            return
        try:
            if path in _POOLED_PATHS:
                self._handle_pooled(target, body)
            else:
                self._forward(target, body)
        finally:
            self.server.dispatch_slots.release()

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

    def _handle_pooled(self, target: str, body: bytes) -> None:
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
        self._forward(target, body, pool, payload)

    def _forward(self, path: str, body: bytes, pool: _Pool | None = None,
                 payload: dict[str, Any] | None = None) -> None:
        route = urlsplit(path).path
        validate = route in _POOLED_PATHS
        request_id = uuid.uuid4().hex[:12]
        deadline = _now() + _POOL_REQUEST_TIMEOUT
        tried: set[str] = set()
        failures: list[dict[str, Any]] = []
        attempt, sweep = 0, 1
        direct_attempts = _DIRECT_ATTEMPTS if validate else 1
        while _now() < deadline:
            if pool is not None and len(tried) >= len(pool.members):
                if sweep >= _POOL_PASSES:
                    break
                time.sleep(min(_SWEEP_BACKOFF * sweep, max(0.0, deadline - _now())))
                sweep += 1
                tried.clear()
            elif pool is None:
                if attempt >= direct_attempts:
                    break
                if attempt:
                    time.sleep(min(_DIRECT_BACKOFF * attempt, max(0.0, deadline - _now())))
            if _now() >= deadline:
                break
            reservation = self.server.scheduler.hold(pool, tried) if pool is not None else nullcontext(None)
            with reservation as member:
                if pool is not None and member is None:
                    break
                attempt += 1
                model = member.model if member is not None else None
                if member is not None:
                    tried.add(member.model)
                outgoing = _rewrite_model(payload, member.model) if member is not None and payload is not None else body
                upstream = None
                committed = False
                try:
                    upstream = _forward_to_upstream("POST", path, _upstream_headers(self.headers, outgoing), outgoing, deadline)
                    verdict = _judge_response(upstream, path=route, deadline=deadline) if validate else None
                    if verdict is None or verdict[0] == "terminal" or (pool is None and verdict[0] not in _INTEGRITY_FAILURES):
                        committed = True
                        clean = self._stream_upstream(upstream, request_id=request_id, member=model, deadline=deadline, path=route)
                        if member is not None and verdict is None:
                            if clean:
                                self.server.runtime_state.clear(member.model)
                            else:
                                self.server.runtime_state.cooldown(member.model, _COOLDOWN_ON_NETERR)
                        return
                    category, delay = verdict
                    if member is not None and category == "rate_limit" and not _has_retry_after(upstream.headers):
                        delay = member.cooldown if member.cooldown is not None else (
                            _COOLDOWN_ON_PACED_429 if member.limit is not None else delay)
                    status = upstream.status
                except (OSError, HTTPException) as error:
                    if committed:
                        return
                    category, delay, status = "network", _COOLDOWN_ON_NETERR, None
                    _LOG.warning("request=%s member=%s category=network error=%s", request_id, _log_text(model), _log_text(error))
                finally:
                    if upstream is not None:
                        upstream.close()
                if member is not None:
                    self.server.runtime_state.cooldown(member.model, delay)
                failures.append({"category": category, "status": status})
                _LOG.warning("request=%s member=%s status=%s category=%s", request_id, _log_text(model), status, category)
        category = "pool_exhausted" if pool is not None else (failures[-1]["category"] if failures else "network")
        message = "router: no pool member succeeded" if pool is not None else (
            "router: upstream unreachable" if category == "network" else "router: upstream response interrupted")
        self._send_json(503 if pool is not None else 502, {"error": {
            "message": message, "type": category, "request_id": request_id, "attempts": failures,
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
                if not self._send_upstream_headers(upstream, len(body)):
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
            scan = upstream.scanner or _SseScan(grammar)
            sent = False
            try:
                for chunk, inspect in chain(((c, False) for c in upstream.prefix),
                                            ((c, True) for c in upstream.body_iter)):
                    had_error = scan.saw_error
                    if inspect:
                        scan.feed(chunk)
                    if scan.verdict is not None and scan.verdict[0] == "oversized":
                        _LOG.warning("request=%s member=%s SSE frame exceeded cap", request_id, _log_text(member))
                        self._write_sse_error()
                        return False
                    try:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    except _CLIENT_DISCONNECT_ERRORS:
                        _LOG.info("request=%s member=%s client disconnected after_bytes=%s", request_id, _log_text(member), sent)
                        return True
                    sent = True
                    if scan.saw_error and not had_error:
                        _LOG.warning("request=%s member=%s forwarded trailing SSE error", request_id, _log_text(member))
            except (OSError, HTTPException, EOFError, zlib.error) as error:
                _LOG.warning("request=%s member=%s upstream SSE interrupted after_bytes=%s: %s", request_id,
                             _log_text(member), sent, _log_text(error))
                self._write_sse_error()
                return False
            if scan.has_content and not scan.terminal and not scan.saw_error:
                _LOG.warning("request=%s member=%s SSE ended without a terminal event", request_id, _log_text(member))
                self._write_sse_error()
                return False
            return not scan.saw_error
        finally:
            upstream.close()

    def _send_upstream_headers(self, upstream: _UpstreamResponse, content_length: int | None) -> bool:
        if any(not _valid_upstream_header(key, value) for key, value in upstream.headers):
            return False
        reason = _status_phrase(upstream.status, upstream.reason)
        self.send_response(upstream.status, reason if _valid_field_value(reason) else "")
        for key, value in upstream.headers:
            lower = key.lower()
            if lower in _RESPONSE_IDENTITY_HEADERS:
                continue
            self.send_header(key, value)
        if content_length is not None:
            self.send_header("Content-Length", str(content_length))
        self.send_header("Connection", "close")
        self.end_headers()
        return True


_RESPONSE_IDENTITY_HEADERS = frozenset({"date", "server"})


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


def _chat_delta_has_content(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return False
    delta = choices[0].get("delta")
    return isinstance(delta, dict) and any(delta.get(key) for key in ("content", "reasoning_content", "tool_calls"))


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


class _SseScan:
    def __init__(self, grammar: _Grammar) -> None:
        self.grammar = grammar
        self.buffer = b""
        self.has_content = False
        self.terminal = False
        self.saw_error = False
        self.verdict: tuple[str, float] | None = None

    def feed(self, chunk: bytes, *, stop_at_content: bool = False) -> bytes:
        self.buffer += chunk
        while match := _SSE_BOUNDARY.search(self.buffer):
            frame, self.buffer = self.buffer[:match.start()], self.buffer[match.end():]
            if len(frame) > _MAX_SSE_FRAME_BYTES:
                self.verdict = "oversized", _COOLDOWN_ON_5XX
                return b""
            event, payload = _sse_parts(frame)
            self.saw_error |= event in _ERROR_EVENTS
            self.terminal |= self.grammar.is_terminal(event)
            if self.verdict is None and not self.has_content:
                if self.grammar.has_content(event, payload):
                    self.has_content = True
                    if stop_at_content:
                        remainder, self.buffer = self.buffer, b""
                        return remainder
                elif event in self.grammar.verdicts:
                    self.verdict = self.grammar.verdicts[event]
        if len(self.buffer) > _MAX_SSE_FRAME_BYTES:
            self.verdict = "oversized", _COOLDOWN_ON_5XX
        return b""


def _validate_stream_head(upstream: _UpstreamResponse, deadline: float,
                          grammar: _Grammar) -> tuple[str, float] | None:
    iterator, chunks, total = iter(upstream.body_iter), [], 0
    scan = _SseScan(grammar)
    try:
        while total < _HEAD_PEEK_BYTES and _now() < deadline:
            chunk = next(iterator)
            chunks.append(chunk)
            total += len(chunk)
            remainder = scan.feed(chunk, stop_at_content=True)
            if remainder:
                chunks[-1] = chunk[:-len(remainder)]
                iterator = chain((remainder,), iterator)
            if scan.verdict is not None or scan.has_content:
                break
    except StopIteration:
        pass
    except (OSError, HTTPException, EOFError, zlib.error):
        return "truncated", _COOLDOWN_ON_NETERR
    upstream.prefix, upstream.body_iter, upstream.scanner = tuple(chunks), iterator, scan
    return scan.verdict or (None if scan.has_content else _EMPTY)


def _validate_body(upstream: _UpstreamResponse, path: str,
                   grammar: _Grammar, deadline: float) -> tuple[str, float] | None:
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
    """Classify the response before the caller commits it to the client."""
    if 200 <= upstream.status < 300:
        grammar = _grammar_for(path)
        if _is_event_stream(upstream.headers):
            return _validate_stream_head(upstream, deadline, grammar)
        return _validate_body(upstream, path, grammar, deadline)
    if upstream.status == 429:
        return "rate_limit", _retry_after_seconds(upstream.headers, _COOLDOWN_ON_429_DEFAULT)
    if upstream.status in _AUTH_STATUS:
        return "auth", _COOLDOWN_ON_AUTH
    if 400 <= upstream.status < 500 and upstream.status not in {404, 408, 409, 425}:
        return "terminal", 0.0
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
    daemon_threads = False
    allow_reuse_address = sys.platform != "win32"

    def __init__(self, address: tuple[str, int], *, state_path: Path | None = None) -> None:
        self.address_family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
        self.dispatch_slots = threading.BoundedSemaphore(_MAX_ACTIVE_REQUESTS)
        self.instance_token = secrets.token_urlsafe(32)
        self.runtime_state = RouterState(state_path, monotonic_time=lambda: _now())
        try:
            super().__init__(address, _RouterHandler)
        except BaseException:
            self.runtime_state.close()
            raise
        self.pools = _PoolRegistry(POOLS_FILE)
        self.scheduler = _Scheduler(self.runtime_state)
        self.models_cache = _ModelListCache()

    def server_close(self) -> None:
        try:
            super().server_close()
        finally:
            self.runtime_state.close()

    def server_bind(self) -> None:
        if sys.platform == "win32":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        socketserver.TCPServer.server_bind(self)
        host = self.server_address[0]
        self.server_name = host.decode() if isinstance(host, (bytes, bytearray)) else host
        self.server_port = self.server_address[1]

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
    server = _RouterServer((ROUTER_HOST, ROUTER_PORT), state_path=ROUTER_STATE_FILE)
    identity = None
    try:
        identity = publish_router_identity(os.getpid(), server.instance_token)
        server.serve_forever(poll_interval=0.05)
    except KeyboardInterrupt:
        _LOG.info("router stopping (KeyboardInterrupt)")
    finally:
        try:
            server.server_close()
        finally:
            if identity is not None:
                clear_router_identity(identity)
    return 0


if __name__ == "__main__":
    sys.exit(run_forever())
