"""In-process pool router that replaces LiteLLM.

Claude Code -> router (this file, :4000, Anthropic /v1/messages) -> CLIProxyAPI (:8317).

Responsibilities kept intentionally small:

    * accept Anthropic Messages API on /v1/messages (streaming or not)
    * accept the count_tokens sibling endpoint
    * accept /v1/models (union of CLIProxyAPI's model list + our pool names)
    * when the request's ``model`` field matches a pool name in pools.json,
      pick a real backend model (rpm-weighted inside a priority tier),
      rewrite the request body, and forward it to CLIProxyAPI
    * on 429 / 5xx / network error: put that backend in cooldown, retry
      with the next-best member up to a bounded number of attempts
    * on any non-pool model: straight passthrough, no rewriting

Runnable directly (``python -m modules.router``) so the launcher can
detach it as a background service that multiple Claudex sessions share.
"""

from __future__ import annotations

import json
import logging
import random
import socket
import sys
import threading
import time
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
    POOLS_FILE,
    PROXY_API_KEY,
    PROXY_HOST,
    PROXY_PORT,
    ROUTER_API_KEY,
    ROUTER_COOLDOWN_429,
    ROUTER_COOLDOWN_5XX,
    ROUTER_COOLDOWN_AUTH,
    ROUTER_COOLDOWN_NETWORK,
    ROUTER_HOST,
    ROUTER_LOG,
    ROUTER_PORT,
)

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

_UPSTREAM_TIMEOUT = 600.0                # seconds; long-running LLM calls
_UPSTREAM_HEADER_TIMEOUT = 60.0          # time to first byte / headers
_MODELS_CACHE_TTL = 30.0                 # seconds for /v1/models cache
_POOLS_STAT_INTERVAL = 0.25              # seconds; how often we recheck mtime
_MAX_RETRY_ATTEMPTS_CAP = 8              # never try more than this per request

_COOLDOWN_ON_429_DEFAULT = ROUTER_COOLDOWN_429
_COOLDOWN_ON_5XX = ROUTER_COOLDOWN_5XX
_COOLDOWN_ON_NETERR = ROUTER_COOLDOWN_NETWORK
_COOLDOWN_ON_AUTH = ROUTER_COOLDOWN_AUTH
_ERROR_PEEK_BYTES = 16 * 1024

_AUTH_STATUS = frozenset({401, 403})

# Hop-by-hop headers we must not forward as-is (RFC 7230 §6.1).
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "content-length",  # we set this ourselves per-attempt
    }
)

_LOG = logging.getLogger("cx.router")


# --------------------------------------------------------------------------- #
# Pool state (loaded lazily and cached with mtime hot-reload)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _Member:
    model: str
    rpm: int          # weight for random selection (defaults to 1)
    priority: int     # lower = tried first (defaults to 0)


@dataclass(frozen=True, slots=True)
class _Pool:
    name: str
    members: tuple[_Member, ...]


class _PoolRegistry:
    """Thread-safe cached view of pools.json with mtime-based hot reload."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._pools: dict[str, _Pool] = {}
        self._mtime_ns: int = -1
        self._last_stat = 0.0

    def get(self, name: str) -> _Pool | None:
        self._refresh_if_changed()
        return self._pools.get(name)

    def names(self) -> list[str]:
        self._refresh_if_changed()
        return sorted(self._pools.keys())

    def _refresh_if_changed(self) -> None:
        now = time.monotonic()
        if now - self._last_stat < _POOLS_STAT_INTERVAL:
            return
        self._last_stat = now
        try:
            mtime_ns = self._path.stat().st_mtime_ns
        except FileNotFoundError:
            with self._lock:
                self._pools = {}
                self._mtime_ns = -1
            return
        if mtime_ns == self._mtime_ns:
            return
        with self._lock:
            if mtime_ns == self._mtime_ns:
                return
            try:
                payload = json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                _LOG.warning("pools.json unreadable, keeping previous state: %s", error)
                return
            self._pools = _parse_pools(payload)
            self._mtime_ns = mtime_ns
            _LOG.info("loaded %d enabled pool(s) from %s", len(self._pools), self._path)


def _parse_pools(payload: Any) -> dict[str, _Pool]:
    pools: dict[str, _Pool] = {}
    if not isinstance(payload, dict):
        return pools
    for raw in payload.get("pools", []) or []:
        if not isinstance(raw, dict):
            continue
        if not raw.get("enabled", True):
            continue
        name = str(raw.get("name", "")).strip()
        if not name:
            continue
        members: list[_Member] = []
        for raw_member in raw.get("members", []) or []:
            if not isinstance(raw_member, dict):
                continue
            model = str(raw_member.get("model", "")).strip()
            if not model:
                continue
            rpm = _coerce_int(raw_member.get("rpm"), default=1, minimum=1)
            priority = _coerce_int(raw_member.get("priority"), default=0, minimum=0)
            members.append(_Member(model=model, rpm=rpm, priority=priority))
        if members:
            pools[name] = _Pool(name=name, members=tuple(members))
    return pools


def _coerce_int(value: Any, *, default: int, minimum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default


# --------------------------------------------------------------------------- #
# Cooldown state
# --------------------------------------------------------------------------- #


class _CooldownTable:
    """Per-backend-model cooldown expiries. Members in cooldown are skipped."""

    def __init__(self) -> None:
        self._until: dict[str, float] = {}
        self._lock = threading.Lock()

    def is_ready(self, model: str) -> bool:
        expiry = self._until.get(model)
        return expiry is None or expiry <= time.monotonic()

    def cooldown(self, model: str, seconds: float, reason: str) -> None:
        seconds = max(1.0, min(seconds, 1800.0))  # clamp 1s..30m
        with self._lock:
            self._until[model] = time.monotonic() + seconds
        _LOG.info("cooldown %ss on %s (%s)", int(seconds), model, reason)

    def clear(self, model: str) -> None:
        with self._lock:
            self._until.pop(model, None)


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #


def _pick_member(
    pool: _Pool,
    cooldowns: _CooldownTable,
    exclude: set[str],
) -> _Member | None:
    """Pick a ready member from ``pool`` honoring priority tiers and RPM weight.

    Strategy: strict priority tiers (lower first). Inside a tier, weighted
    random by ``rpm``. Members already tried this request (``exclude``) and
    those in cooldown are skipped.
    """
    ready = [
        m for m in pool.members
        if m.model not in exclude and cooldowns.is_ready(m.model)
    ]
    if not ready:
        return None
    top_priority = min(m.priority for m in ready)
    tier = [m for m in ready if m.priority == top_priority]
    if len(tier) == 1:
        return tier[0]
    total = sum(m.rpm for m in tier)
    r = random.uniform(0.0, total)
    cursor = 0.0
    for member in tier:
        cursor += member.rpm
        if r <= cursor:
            return member
    return tier[-1]  # rounding safety net


# --------------------------------------------------------------------------- #
# Upstream forwarding
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class _UpstreamResponse:
    status: int
    reason: str
    headers: list[tuple[str, str]]
    body_iter: Iterable[bytes]
    connection: HTTPConnection            # kept alive for streaming reads
    _closed: bool = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.connection.close()
        except OSError:
            pass


def _forward_to_upstream(
    method: str,
    path: str,
    headers: dict[str, str],
    body: bytes,
) -> _UpstreamResponse:
    conn = HTTPConnection(PROXY_HOST, PROXY_PORT, timeout=_UPSTREAM_TIMEOUT)
    conn.request(method, path, body=body, headers=headers)
    conn.sock.settimeout(_UPSTREAM_HEADER_TIMEOUT)  # only headers timeout short
    response = conn.getresponse()
    body_socket = conn.sock
    if body_socket is None:
        body_socket = getattr(getattr(response.fp, "raw", None), "_sock", None)
    if body_socket is not None:
        body_socket.settimeout(_UPSTREAM_TIMEOUT)   # then long for body reads

    def _iter_body() -> Iterable[bytes]:
        # Use read1(): return whatever bytes are immediately available (up to
        # the given size), without blocking to fill an 8KiB buffer. This is
        # what makes SSE actually stream — Anthropic events are small
        # (~100–200 bytes) and arrive one at a time, so a fill-first read()
        # would coalesce many seconds of events into one chunk before
        # yielding, defeating the purpose of streaming.
        try:
            while True:
                chunk = response.read1(65536)
                if not chunk:
                    return
                yield chunk
        finally:
            try:
                response.close()
            except OSError:
                pass

    return _UpstreamResponse(
        status=response.status,
        reason=response.reason or "",
        headers=[(k, v) for (k, v) in response.getheaders()
                 if k.lower() not in _HOP_BY_HOP],
        body_iter=_iter_body(),
        connection=conn,
    )


# --------------------------------------------------------------------------- #
# Model list (proxied from CLIProxyAPI + pool aliases injected)
# --------------------------------------------------------------------------- #


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
            if self._payload is not None and now - self._fetched_at < _MODELS_CACHE_TTL:
                return self._payload
            payload = self._fetch()
            self._payload = payload
            self._fetched_at = time.monotonic()
            return payload

    def _fetch(self) -> dict[str, Any]:
        conn = HTTPConnection(PROXY_HOST, PROXY_PORT, timeout=5.0)
        try:
            conn.request(
                "GET",
                "/v1/models",
                headers={"Authorization": f"Bearer {PROXY_API_KEY}"},
            )
            response = conn.getresponse()
            data = response.read()
            if response.status != 200:
                _LOG.warning("upstream /v1/models returned %s", response.status)
                return {"object": "list", "data": []}
            return json.loads(data.decode("utf-8"))
        except (OSError, HTTPException, json.JSONDecodeError) as error:
            _LOG.warning("upstream /v1/models fetch failed: %s", error)
            return {"object": "list", "data": []}
        finally:
            try:
                conn.close()
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# HTTP handler
# --------------------------------------------------------------------------- #


class _RouterHandler(BaseHTTPRequestHandler):
    server_version = "cx-router/1.0"
    protocol_version = "HTTP/1.1"

    # BaseHTTPRequestHandler default logging spams stderr. Route to our logger.
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        _LOG.debug("%s - %s", self.address_string(), format % args)

    def log_error(self, format: str, *args: Any) -> None:  # noqa: A002
        _LOG.info("%s - %s", self.address_string(), format % args)

    # ---- routing -------------------------------------------------------------

    def do_GET(self) -> None:                 # noqa: N802 (BaseHTTPRequestHandler API)
        path = urlsplit(self.path).path
        if path in {"/", "/health", "/-/ready", "/-/health"}:
            self._send_json(200, {"status": "ok"})
            return
        if path == "/v1/models":
            self._require_auth() and self._handle_models()
            return
        self._send_json(404, {"error": {"message": f"unknown path: {path}"}})

    def do_POST(self) -> None:                # noqa: N802
        path = urlsplit(self.path).path
        if path == "/v1/messages":
            if self._require_auth():
                self._handle_messages(count_tokens=False)
            return
        if path == "/v1/messages/count_tokens":
            if self._require_auth():
                self._handle_messages(count_tokens=True)
            return
        # OpenAI clients occasionally hit /v1/chat/completions against the same
        # base URL — forward those as-is (no pool routing, model rewrites off).
        if path in {"/v1/chat/completions", "/v1/completions"}:
            if self._require_auth():
                self._handle_passthrough(path)
            return
        self._send_json(404, {"error": {"message": f"unknown path: {path}"}})

    # ---- auth ----------------------------------------------------------------

    def _require_auth(self) -> bool:
        header = (self.headers.get("Authorization") or "").strip()
        api_key = self.headers.get("x-api-key", "").strip()
        expected = ROUTER_API_KEY
        if header.startswith("Bearer "):
            token = header[len("Bearer "):]
            if token == expected:
                return True
        if api_key and api_key == expected:
            return True
        self._send_json(401, {"error": {"message": "invalid api key"}})
        return False

    # ---- /v1/models ----------------------------------------------------------

    def _handle_models(self) -> None:
        cache: _ModelListCache = self.server.models_cache        # type: ignore[attr-defined]
        registry: _PoolRegistry = self.server.pools              # type: ignore[attr-defined]
        payload = dict(cache.get())
        data = list(payload.get("data") or [])
        upstream_ids = {str(m.get("id", "")).strip() for m in data if isinstance(m, dict)}
        created = int(time.time())
        for name in registry.names():
            if name in upstream_ids:
                continue
            data.append({"id": name, "object": "model", "created": created, "owned_by": "pool"})
        payload["object"] = "list"
        payload["data"] = data
        self._send_json(200, payload)

    # ---- /v1/messages --------------------------------------------------------

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

        registry: _PoolRegistry = self.server.pools              # type: ignore[attr-defined]
        cooldowns: _CooldownTable = self.server.cooldowns        # type: ignore[attr-defined]
        pool = registry.get(requested_model)

        upstream_path = "/v1/messages/count_tokens" if count_tokens else "/v1/messages"

        if pool is None:
            # not a pool name → straight passthrough with the original body
            self._forward_once(upstream_path, dict(self.headers), body)
            return

        self._forward_pool(pool, upstream_path, body, payload)

    def _forward_pool(
        self,
        pool: _Pool,
        upstream_path: str,
        body: bytes,
        payload: dict[str, Any],
    ) -> None:
        cooldowns: _CooldownTable = self.server.cooldowns        # type: ignore[attr-defined]
        max_attempts = min(_MAX_RETRY_ATTEMPTS_CAP, len(pool.members))
        tried: set[str] = set()
        failures: list[dict[str, Any]] = []

        for attempt in range(1, max_attempts + 1):
            member = _pick_member(pool, cooldowns, exclude=tried)
            if member is None:
                break
            tried.add(member.model)

            rewritten = _rewrite_model(body, payload, member.model)
            headers = _upstream_headers(self.headers, rewritten)
            _LOG.info(
                "attempt %d/%d: pool=%s -> %s",
                attempt, max_attempts, pool.name, member.model,
            )
            try:
                upstream = _forward_to_upstream("POST", upstream_path, headers, rewritten)
            except (OSError, HTTPException):
                cooldowns.cooldown(member.model, _COOLDOWN_ON_NETERR, "network")
                failures.append({"category": "network"})
                _LOG.info("pool=%s model=%s failed category=network", pool.name, member.model)
                continue

            retry = _classify_retry(upstream)
            if retry is not None:
                category, delay = retry
                upstream.close()
                cooldowns.cooldown(member.model, delay, category)
                failure: dict[str, Any] = {
                    "category": category,
                    "status": upstream.status,
                }
                failures.append(failure)
                _LOG.info(
                    "pool=%s model=%s failed status=%d category=%s",
                    pool.name, member.model, upstream.status, category,
                )
                continue

            cooldowns.clear(member.model)
            self._stream_upstream(upstream)
            return

        _LOG.warning("pool %s exhausted after %d attempt(s)", pool.name, len(tried))
        self._send_json(
            503,
            {
                "error": {
                    "message": "router: no pool member succeeded",
                    "type": "pool_exhausted",
                    "attempts": failures,
                }
            },
        )

    # ---- passthrough (chat/completions etc) ---------------------------------

    def _handle_passthrough(self, path: str) -> None:
        body = self._read_body()
        if body is None:
            return
        headers = _upstream_headers(self.headers, body)
        try:
            upstream = _forward_to_upstream("POST", path, headers, body)
        except (OSError, HTTPException) as error:
            self._send_json(502, {"error": {"message": f"router: upstream unreachable: {error}"}})
            return
        self._stream_upstream(upstream)

    def _forward_once(self, path: str, incoming_headers: dict[str, str], body: bytes) -> None:
        headers = _upstream_headers(incoming_headers, body)
        try:
            upstream = _forward_to_upstream("POST", path, headers, body)
        except (OSError, HTTPException) as error:
            self._send_json(502, {"error": {"message": f"router: upstream unreachable: {error}"}})
            return
        self._stream_upstream(upstream)

    # ---- helpers -------------------------------------------------------------

    def _read_body(self) -> bytes | None:
        length_hdr = self.headers.get("Content-Length")
        if length_hdr is None:
            return b""
        try:
            length = int(length_hdr)
        except ValueError:
            self._send_json(400, {"error": {"message": "invalid Content-Length"}})
            return None
        if length < 0 or length > 128 * 1024 * 1024:
            self._send_json(413, {"error": {"message": "request too large"}})
            return None
        return self.rfile.read(length) if length else b""

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

    def _stream_upstream(self, upstream: _UpstreamResponse) -> None:
        try:
            reason_bytes = HTTPStatus(upstream.status).phrase if 100 <= upstream.status < 600 else upstream.reason
            self.send_response(upstream.status, reason_bytes)
            for key, value in upstream.headers:
                self.send_header(key, value)
            # We do our own framing: send chunks as they come, close on end.
            self.send_header("Connection", "close")
            self.end_headers()
            for chunk in upstream.body_iter:
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
        finally:
            upstream.close()


# --------------------------------------------------------------------------- #
# Header + body helpers
# --------------------------------------------------------------------------- #


def _rewrite_model(original_body: bytes, parsed: dict[str, Any], real_model: str) -> bytes:
    """Return a JSON body identical to ``original_body`` but with model overridden.

    We re-serialize ``parsed`` rather than doing string surgery so we can't
    corrupt a body that legitimately contains the pool name inside a message.
    """
    parsed = dict(parsed)  # shallow copy — we only change the top-level key
    parsed["model"] = real_model
    return json.dumps(parsed, ensure_ascii=False).encode("utf-8")


def _upstream_headers(incoming: Any, body: bytes) -> dict[str, str]:
    """Build headers for the upstream request.

    Strips hop-by-hop headers, drops the client's Authorization / x-api-key,
    replaces them with CLIProxyAPI's key, and fixes Content-Length to the
    (possibly rewritten) body length.
    """
    out: dict[str, str] = {}
    for key in incoming.keys() if hasattr(incoming, "keys") else []:
        lower = key.lower()
        if lower in _HOP_BY_HOP:
            continue
        if lower in {"authorization", "x-api-key", "host"}:
            continue
        value = incoming[key] if hasattr(incoming, "__getitem__") else incoming.get(key)
        if value is None:
            continue
        out[key] = value
    out["Content-Type"] = out.get("Content-Type", "application/json")
    out["Content-Length"] = str(len(body))
    out["Host"] = f"{PROXY_HOST}:{PROXY_PORT}"
    # Anthropic-style auth (CLIProxyAPI accepts both).
    out["Authorization"] = f"Bearer {PROXY_API_KEY}"
    out["x-api-key"] = PROXY_API_KEY
    out.setdefault("anthropic-version", "2023-06-01")
    out.setdefault("Accept", "application/json, text/event-stream")
    return out


def _retry_after_seconds(headers: list[tuple[str, str]], default: float) -> float:
    for key, value in headers:
        if key.lower() != "retry-after":
            continue
        try:
            delay = float(value)
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value)
            except (TypeError, ValueError, OverflowError):
                return default
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            delay = retry_at.timestamp() - time.time()
        return max(1.0, min(delay, 1800.0))
    return default


def _peek_body_chunk(upstream: _UpstreamResponse) -> bytes:
    iterator = iter(upstream.body_iter)
    try:
        chunk = next(iterator)
    except StopIteration:
        return b""
    upstream.body_iter = chain((chunk,), iterator)
    return chunk


def _classify_retry(upstream: _UpstreamResponse) -> tuple[str, float] | None:
    """Classify a pooled-member response using protocol signals, not body text."""
    status = upstream.status

    if 200 <= status < 300:
        if _is_event_stream(upstream.headers):
            data = _peek_body_chunk(upstream)
            normalized = " ".join(data.decode("utf-8", errors="replace").lower().split())
            if _looks_like_stream_error(normalized):
                return "stream_error", _COOLDOWN_ON_5XX
        return None

    _drain_error_body(upstream)
    if status == 429:
        return (
            "rate_limit",
            _retry_after_seconds(upstream.headers, _COOLDOWN_ON_429_DEFAULT),
        )
    if status in _AUTH_STATUS:
        return "auth", _COOLDOWN_ON_AUTH
    if 400 <= status < 500:
        return "rejected", _COOLDOWN_ON_5XX
    return "upstream", _COOLDOWN_ON_5XX


def _is_event_stream(headers: list[tuple[str, str]]) -> bool:
    return any(
        key.lower() == "content-type" and "text/event-stream" in value.lower()
        for key, value in headers
    )


def _looks_like_stream_error(text: str) -> bool:
    return (
        "event: error" in text
        or '"type":"error"' in text
        or '"type": "error"' in text
    )


def _drain_error_body(upstream: _UpstreamResponse, max_bytes: int = 400) -> None:
    """Drain a bounded prefix so the connection can be closed cleanly."""
    remaining = max_bytes
    try:
        for chunk in upstream.body_iter:
            remaining -= len(chunk)
            if remaining <= 0:
                break
    except (OSError, HTTPException):
        pass


# --------------------------------------------------------------------------- #
# Server assembly + entry point
# --------------------------------------------------------------------------- #


class _RouterServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int]) -> None:
        super().__init__(address, _RouterHandler)
        self.pools = _PoolRegistry(POOLS_FILE)
        self.cooldowns = _CooldownTable()
        self.models_cache = _ModelListCache()


def _wait_upstream(deadline: float) -> None:
    """Block until CLIProxyAPI's port is open, or until the deadline elapses."""
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((PROXY_HOST, PROXY_PORT), timeout=0.5):
                return
        except OSError:
            time.sleep(0.3)
    raise RuntimeError(
        f"CLIProxyAPI unreachable at {PROXY_HOST}:{PROXY_PORT} — start it before the router."
    )


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
    """Start the router in the current process and block until killed."""
    _configure_logging()
    _LOG.info(
        "router starting on %s:%d, forwarding to %s:%d",
        ROUTER_HOST, ROUTER_PORT, PROXY_HOST, PROXY_PORT,
    )
    _wait_upstream(deadline=time.monotonic() + 30.0)
    server = _RouterServer((ROUTER_HOST, ROUTER_PORT))
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        _LOG.info("router stopping (KeyboardInterrupt)")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover — module entry point
    sys.exit(run_forever())
