from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import time
import types
import unittest
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from modules.router import (
    _Member,
    _parse_pools,
    _Pool,
    _PoolRegistry,
    _retry_after_seconds,
    _rewrite_model,
    _RouterServer,
    _Scheduler,
    _upstream_headers,
)
from modules.router_state import RouterState

_SSE = {"Content-Type": "text/event-stream"}
_OK_BODY = b'{"type":"message","role":"assistant","content":[{"type":"text","text":"ok"}]}'
_START = b'event: message_start\ndata: {"type":"message_start"}\n\n'
_DELTA = b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}\n\n'
_STOP = b'event: message_stop\ndata: {"type":"message_stop"}\n\n'


def _mk_pool(*members: tuple[str, int, int], strategy: str = "fill-first") -> _Pool:
    return _Pool(
        name="p",
        members=tuple(_Member(model=m, rpm=r, priority=p) for m, r, p in members),
        strategy=strategy,
    )


class _FakeUpstreamServer(ThreadingHTTPServer):
    def handle_error(self, request: object, client_address: object) -> None:
        if isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)


class _UpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        state = self.server.state
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        state.models.append(payload["model"])
        state.paths.append(self.path)
        try:
            status, headers, body = state.responses.popleft()
        except IndexError:
            body = json.dumps({"error": "test upstream ran out of scripted responses"}).encode()
            self.close_connection = True
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            state.request_started.set()
            return
        if status == 0:
            self.close_connection = True
            return
        state.request_started.set()
        if response_delay := getattr(state, "response_delay", 0):
            state.request_started.clear()
            time.sleep(response_delay)
            state.request_started.set()
        if header_delay := getattr(state, "header_delay", 0):
            self.close_connection = True
            lines = [f"HTTP/1.1 {status} OK\r\n"]
            lines.extend(f"{key}: {value}\r\n" for key, value in headers.items())
            if isinstance(body, bytes) and not any(key.lower() == "content-length" for key in headers):
                lines.append(f"Content-Length: {len(body)}\r\n")
            else:
                lines.append("Connection: close\r\n")
            for line in lines:
                self.wfile.write(line.encode())
                self.wfile.flush()
                time.sleep(header_delay)
            self.wfile.write(b"\r\n")
            self.wfile.flush()
        else:
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            if isinstance(body, bytes):
                self.send_header("Content-Length", str(len(body)))
            else:
                self.close_connection = True
                self.send_header("Connection", "close")
            self.end_headers()
        if isinstance(body, bytes):
            self.wfile.write(body)
            state.request_started.set()
            return
        self.wfile.write(body[0])
        self.wfile.flush()
        state.stream_gate.wait(timeout=1)
        for chunk in body[1:]:
            if stream_delay := getattr(state, "stream_delay", 0):
                time.sleep(stream_delay)
            if chunk == b"__drop__":
                self.connection.close()
                return
            self.wfile.write(chunk)
            self.wfile.flush()
        state.request_started.set()


@contextmanager
def _running_router(
    *responses: tuple[int, dict[str, str], bytes | tuple[bytes, ...]],
    members: list[dict[str, object]] | None = None,
    strategy: str = "fill-first",
    persistent_state: bool = False,
):
    upstream = _FakeUpstreamServer(("127.0.0.1", 0), _UpstreamHandler)
    upstream.state = types.SimpleNamespace(
        responses=deque(responses), models=[], paths=[], stream_gate=threading.Event(),
        request_started=threading.Event(), stream_delay=0, response_delay=0, header_delay=0)
    upstream_thread = threading.Thread(
        target=upstream.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    upstream_thread.start()

    member_specs = members or [
        {"model": "provider/first", "priority": 1},
        {"model": "provider/second", "priority": 2},
    ]

    with tempfile.TemporaryDirectory() as temporary:
        pools_file = Path(temporary) / "pools.json"
        pools_file.write_text(
            json.dumps(
                {
                    "version": 1,
                    "pools": [
                    {
                        "name": "test-pool",
                        "strategy": strategy,
                        "members": member_specs,
                    }
                    ],
                }
            ),
            encoding="utf-8",
        )
        with (
            patch("modules.router.PROXY_HOST", "127.0.0.1"),
            patch("modules.router.PROXY_PORT", upstream.server_port),
            patch("modules.router.ROUTER_API_KEY", "test-router-key"),
        ):
            state_path = pools_file.with_name("router-state.json") if persistent_state else None
            router = _RouterServer(("127.0.0.1", 0), state_path=state_path)
            router.pools = _PoolRegistry(pools_file)
            router.upstream_state = upstream.state
            router_thread = threading.Thread(
                target=router.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
            )
            router_thread.start()
            try:
                yield router
            finally:
                router.shutdown()
                router.server_close()
                router_thread.join(timeout=2)

    upstream.shutdown()
    upstream.server_close()
    upstream_thread.join(timeout=2)


def _post(
    router: _RouterServer,
    path: str = "/v1/messages",
    model: str = "test-pool",
) -> tuple[int, bytes]:
    body = json.dumps({"model": model, "messages": []}).encode("utf-8")
    connection = http.client.HTTPConnection("127.0.0.1", router.server_port, timeout=10)
    connection.request(
        "POST",
        path,
        body=body,
        headers={
            "Authorization": "Bearer test-router-key",
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
        },
    )
    response = connection.getresponse()
    result = response.status, response.read()
    connection.close()
    return result


class ParsePoolsTests(unittest.TestCase):
    def test_defaults_applied(self) -> None:
        payload = {
            "pools": [
                {
                    "name": "p",
                    "enabled": True,
                    "members": [{"model": "a/x"}, {"model": "b/x", "rpm": 5, "priority": 2}],
                }
            ]
        }
        pools = _parse_pools(payload)
        members = pools["p"].members
        self.assertEqual(members[0].rpm, 1)
        self.assertEqual(members[0].priority, 0)
        self.assertEqual(members[1].rpm, 5)
        self.assertEqual(members[1].priority, 2)

    def test_disabled_pools_skipped(self) -> None:
        payload = {"pools": [{"name": "off", "enabled": False, "members": [{"model": "a/x"}]}]}
        self.assertEqual(_parse_pools(payload), {})

    def test_strategy_parsed_with_fallback(self) -> None:
        for strategy in ("fill-first", "round-robin", "weighted", "least-busy"):
            payload = {"pools": [{"name": "p", "members": [{"model": "a/x"}], "strategy": strategy}]}
            with self.subTest(strategy=strategy):
                self.assertEqual(_parse_pools(payload)["p"].strategy, strategy)
        bad = {"pools": [{"name": "p", "members": [{"model": "a/x"}], "strategy": "chaos"}]}
        self.assertEqual(_parse_pools(bad)["p"].strategy, "fill-first")


def _pick(scheduler: _Scheduler, pool: _Pool, tried: set[str] | None = None):
    with scheduler.hold(pool, tried or set()) as member:
        return member.model if member is not None else None


class SelectionTests(unittest.TestCase):
    def test_fill_first_drains_priority_before_weight(self):
        scheduler = _Scheduler(RouterState())
        pool = _mk_pool(("low", 999, 2), ("top", 1, 0))
        self.assertEqual([_pick(scheduler, pool) for _ in range(10)], ["top"] * 10)
        self.assertEqual(_pick(scheduler, pool, {"top"}), "low")
        scheduler.state.cooldown("top", 30)
        self.assertEqual(_pick(scheduler, pool), "low")

    def test_every_strategy_can_retry_cooling_members(self):
        for strategy in ("round-robin", "fill-first", "weighted", "least-busy"):
            with self.subTest(strategy=strategy):
                scheduler = _Scheduler(RouterState())
                pool = _mk_pool(("a", 1, 0), ("b", 1, 1), strategy=strategy)
                scheduler.state.cooldown("a", 30)
                scheduler.state.cooldown("b", 30)
                first = _pick(scheduler, pool)
                self.assertIn(first, {"a", "b"})
                self.assertEqual(_pick(scheduler, pool, {first}), ({"a", "b"} - {first}).pop())

    def test_capped_ready_members_do_not_hide_cooling_capacity(self):
        for strategy in ("round-robin", "fill-first", "weighted", "least-busy"):
            scheduler = _Scheduler(RouterState())
            pool = _Pool("p", (_Member("a", 1, 0, 1), _Member("b", 1, 1, 1)), strategy)
            scheduler.state.reserve("a", 1)
            scheduler.state.cooldown("b", 30)
            self.assertEqual(_pick(scheduler, pool), "b")
            self.assertIsNone(_pick(scheduler, pool))

    def test_round_robin_advances_once_per_request(self):
        scheduler = _Scheduler(RouterState())
        pool = _mk_pool(("a", 1, 0), ("b", 1, 0), ("c", 1, 0), strategy="round-robin")
        self.assertEqual(_pick(scheduler, pool), "a")
        self.assertEqual(_pick(scheduler, pool, {"a"}), "b")
        self.assertEqual([_pick(scheduler, pool) for _ in range(3)], ["b", "c", "a"])

    def test_round_robin_skips_and_restores_cooling_member(self):
        scheduler = _Scheduler(RouterState())
        pool = _mk_pool(("a", 1, 0), ("b", 1, 0), ("c", 1, 0), strategy="round-robin")
        scheduler.state.cooldown("b", 30)
        self.assertEqual([_pick(scheduler, pool) for _ in range(4)], ["a", "c", "a", "c"])
        scheduler.state.clear("b")
        self.assertEqual([_pick(scheduler, pool) for _ in range(3)], ["a", "b", "c"])

    def test_configuration_reorder_resets_the_cursor(self):
        scheduler = _Scheduler(RouterState())
        pool = _mk_pool(("a", 1, 0), ("b", 1, 0), strategy="round-robin")
        self.assertEqual(_pick(scheduler, pool), "a")
        reordered = _Pool(pool.name, tuple(reversed(pool.members)), pool.strategy)
        self.assertEqual(_pick(scheduler, reordered), "b")

    def test_quota_expires_against_the_clock(self):
        clock = [0.0]
        scheduler = _Scheduler(RouterState(monotonic_time=lambda: clock[0]))
        pool = _Pool("p", (_Member("a", 1, 0, 1),))
        self.assertEqual(_pick(scheduler, pool), "a")
        self.assertIsNone(_pick(scheduler, pool))
        clock[0] = 61
        self.assertEqual(_pick(scheduler, pool), "a")

    def test_concurrent_requests_cannot_overrun_quota(self):
        scheduler = _Scheduler(RouterState())
        pool = _Pool("p", (_Member("a", 1, 0, 1),))
        with ThreadPoolExecutor(max_workers=8) as executor:
            picks = list(executor.map(lambda _: _pick(scheduler, pool), range(16)))
        self.assertEqual(picks.count("a"), 1)
        self.assertEqual(picks.count(None), 15)

    def test_least_busy_reserves_load_with_selection_and_releases_on_error(self):
        scheduler = _Scheduler(RouterState())
        pool = _mk_pool(("a", 1, 0), ("b", 1, 9), strategy="least-busy")
        with scheduler.hold(pool, set()) as first:
            self.assertEqual(scheduler.count(first.model), 1)
            self.assertEqual(_pick(scheduler, pool), ({"a", "b"} - {first.model}).pop())
        self.assertEqual(scheduler.count(first.model), 0)
        with self.assertRaises(RuntimeError), scheduler.hold(pool, set()):
            raise RuntimeError("probe")
        self.assertEqual(sum(scheduler.count(m.model) for m in pool.members), 0)

    def test_weighted_selection_ignores_priority_and_handles_large_weights(self):
        import random
        scheduler = _Scheduler(RouterState())
        pool = _mk_pool(("a", 2**63 - 1, 0), ("b", 2**63 - 1, 9), strategy="weighted")
        with patch("modules.router.random.random", side_effect=random.Random(123).random):
            picks = [_pick(scheduler, pool) for _ in range(100)]
        self.assertGreater(picks.count("a"), 20)
        self.assertGreater(picks.count("b"), 20)


class ModelRewriteTests(unittest.TestCase):
    def test_only_top_level_model_field_is_replaced(self) -> None:
        body = json.dumps(
            {
                "model": "my-pool",
                "messages": [
                    {"role": "user", "content": "hi, tell me about my-pool"},
                ],
            }
        ).encode("utf-8")
        parsed = json.loads(body)
        rewritten = _rewrite_model(parsed, "real/model")
        out = json.loads(rewritten)
        self.assertEqual(out["model"], "real/model")
        self.assertIn("my-pool", out["messages"][0]["content"])


class HeaderRewriteTests(unittest.TestCase):
    def test_hop_and_auth_are_stripped_before_forwarding(self) -> None:
        incoming = {
            "Authorization": "Bearer client-key",
            "x-api-key": "client-key",
            "Host": "127.0.0.1:4000",
            "Content-Length": "999",
            "Connection": "keep-alive",
            "X-Custom": "keep-me",
        }
        headers = _upstream_headers(incoming, body=b'{"a":1}')
        self.assertNotIn("Connection", headers)
        self.assertEqual(headers["X-Custom"], "keep-me")
        self.assertEqual(headers["Content-Length"], "7")
        self.assertTrue(headers["Authorization"].startswith("Bearer "))
        self.assertIn("x-api-key", headers)
        self.assertNotEqual(headers["Host"], "127.0.0.1:4000")


class RetryAfterTests(unittest.TestCase):
    def test_http_date_is_supported(self) -> None:
        with patch("modules.router.time.time", return_value=1_700_000_000):
            delay = _retry_after_seconds(
                [("Retry-After", "Tue, 14 Nov 2023 22:15:20 GMT")],
                default=60,
            )
        self.assertEqual(delay, 120)


class PoolFailoverTests(unittest.TestCase):
    def test_dispatch_limit_preserves_health_and_authentication(self) -> None:
        with patch("modules.router._MAX_ACTIVE_REQUESTS", 1), _running_router(
            (200, _SSE, (_START, _DELTA, _STOP)),
        ) as router:
            with ThreadPoolExecutor(max_workers=1) as executor:
                first = executor.submit(_post, router)
                self.assertTrue(router.upstream_state.request_started.wait(2))
                try:
                    connection = http.client.HTTPConnection("127.0.0.1", router.server_port, timeout=5)
                    connection.request("GET", "/health")
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    response.read()
                    connection.close()
                    connection = http.client.HTTPConnection("127.0.0.1", router.server_port, timeout=5)
                    connection.request("POST", "/v1/messages", body=b"{}")
                    response = connection.getresponse()
                    self.assertEqual(response.status, 401)
                    response.read()
                    connection.close()
                    status, body = _post(router)
                finally:
                    router.upstream_state.stream_gate.set()
                first_status, first_body = first.result(timeout=5)

        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body)["error"]["message"], "router: capacity exhausted")
        self.assertEqual((first_status, first_body), (200, _START + _DELTA + _STOP))
        self.assertEqual(router.upstream_state.models, ["provider/first"])

    def test_snapshot_write_failure_does_not_prevent_upstream_dispatch(self) -> None:
        with patch("modules.router_state._publish_state", side_effect=OSError("disk full")):
            with _running_router(
                (200, {}, _OK_BODY),
                members=[{"model": "provider/first", "rpm": 1, "priority": 1}],
                persistent_state=True,
            ) as router:
                status, body = _post(router)

        self.assertEqual((status, body), (200, _OK_BODY))
        self.assertEqual(router.upstream_state.models, ["provider/first"])

    def test_a_rejected_attempt_fails_over_to_the_next_member(self) -> None:
        cases = (
            ("auth_401", (401, {}, b'{"error":{"message":"API Key error"}}')),
            ("auth_403", (403, {}, b'{"error":{"message":"API Key error"}}')),
            ("server_error", (500, {}, b'{"error":{"message":"provider failed"}}')),
            ("transport_closed", (0, {}, b"")),
            ("transient_408", (408, {}, b'{"error":{"message":"transient"}}')),
            ("transient_409", (409, {}, b'{"error":{"message":"transient"}}')),
            ("transient_425", (425, {}, b'{"error":{"message":"transient"}}')),
            ("missing_route_404", (404, {}, b'{"error":{"message":"model not found"}}')),
        )
        for label, rejected in cases:
            with self.subTest(rejection=label), _running_router(
                rejected, (200, {}, _OK_BODY)
            ) as router:
                status, body = _post(router)

            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), json.loads(_OK_BODY))
            self.assertEqual(
                router.upstream_state.models,
                ["provider/first", "provider/second"],
            )

    def test_a_stream_the_upstream_abandons_tries_the_next_member(self) -> None:
        cases = (
            ("error_event_api_key", _SSE,
             b'event: error\ndata: {"type":"error","error":{"message":"API Key error"}}\n\n'),
            ("error_event_unknown_failure", _SSE,
             b'event: error\ndata: {"type":"error","error":{"message":"unknown provider failure"}}\n\n'),
            ("abandoned_mid_stream", {**_SSE, "Content-Length": "999"},
             (_START, b"__drop__")),
        )
        for label, headers, broken in cases:
            with self.subTest(stream=label), _running_router(
                (200, headers, broken), (200, {}, _OK_BODY)
            ) as router:
                router.upstream_state.stream_gate.set()
                status, body = _post(router)

            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), json.loads(_OK_BODY))
            self.assertEqual(
                router.upstream_state.models,
                ["provider/first", "provider/second"],
            )

    def test_terminal_pool_4xx_is_forwarded_without_failover_or_cooldown(self) -> None:
        for status_code in (400, 422):
            with self.subTest(status=status_code):
                body = b'{"error":{"message":"arbitrary rejection"}}'
                with _running_router(
                    (status_code, {"X-Provider-Error": "request-rejected"}, body),
                    (200, {}, _OK_BODY),
                ) as router:
                    status, body = _post(router)

                self.assertEqual(status, status_code)
                self.assertEqual(body, b'{"error":{"message":"arbitrary rejection"}}')
                self.assertEqual(router.upstream_state.models, ["provider/first"])
                self.assertTrue(router.runtime_state.is_ready("provider/first"))
                self.assertTrue(router.runtime_state.is_ready("provider/second"))

    def test_pooled_failure_does_not_log_provider_body(self) -> None:
        provider_secret = b"provider-body-secret"
        with self.assertLogs("cx.router", level="WARNING") as logs:
            with _running_router(
                (500, {}, provider_secret),
                (200, {}, _OK_BODY),
            ) as router:
                status, _ = _post(router)
        self.assertEqual(status, 200)
        output = "\n".join(logs.output)
        self.assertNotIn(provider_secret.decode(), output)

    def test_direct_failure_logs_path_without_query(self) -> None:
        query_secret = "query-secret-7f93"
        with self.assertLogs("cx.router", level="DEBUG") as logs:
            with _running_router(
                (500, {}, b'{"error":{"message":"provider failed"}}'),
            ) as router:
                status, _ = _post(router, f"/v1/completions?token={query_secret}", model="provider/direct")

        self.assertEqual(status, 500)
        output = "\n".join(logs.output)
        self.assertNotIn(query_secret, output)
        self.assertIn("/v1/completions", output)

    def test_count_tokens_rate_limit_tries_next_member(self) -> None:
        with _running_router(
            (429, {"Retry-After": "1"}, b'{"error":{"message":"Rate limit exceeded"}}'),
            (200, {}, b'{"input_tokens":7}'),
        ) as router:
            status, body = _post(router, "/v1/messages/count_tokens")

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"input_tokens": 7})
        self.assertEqual(router.upstream_state.models, ["provider/first", "provider/second"])
        self.assertEqual(
            router.upstream_state.paths,
            ["/v1/messages/count_tokens", "/v1/messages/count_tokens"],
        )

    def test_pooled_and_passthrough_routes_preserve_exact_query_strings(self) -> None:
        routes = (
            ("/v1/messages?beta=true&x=%2F", "test-pool"),
            ("/v1/completions?beta=true&x=%2F", "provider/direct"),
        )
        for path, model in routes:
            with self.subTest(path=path), _running_router((200, {}, _OK_BODY)) as router:
                status, _ = _post(router, path, model=model)
                self.assertEqual(status, 200)
                self.assertEqual(router.upstream_state.paths, [path])

    def test_successful_stream_is_forwarded_incrementally(self) -> None:
        with _running_router(
            (200, _SSE, (_START + _DELTA, _STOP)),
        ) as router:
            body = json.dumps({"model": "test-pool", "messages": []}).encode("utf-8")
            connection = http.client.HTTPConnection("127.0.0.1", router.server_port, timeout=10)
            connection.request(
                "POST",
                "/v1/messages",
                body=body,
                headers={
                    "Authorization": "Bearer test-router-key",
                    "Content-Type": "application/json",
                    "Content-Length": str(len(body)),
                },
            )
            response = connection.getresponse()
            first = response.read(len(_START + _DELTA))
            router.upstream_state.stream_gate.set()
            rest = response.read()
            connection.close()

        self.assertEqual(response.status, 200)
        self.assertEqual(first, _START + _DELTA)
        self.assertEqual(rest, _STOP)
        self.assertEqual(router.upstream_state.models, ["provider/first"])

    def test_stream_is_stopped_at_the_whole_request_deadline(self) -> None:
        final_delta = (
            b'event: content_block_delta\ndata: '
            b'{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"final"}}\n\n'
        )
        chunks = (_START, _DELTA, *([_DELTA] * 8), final_delta, _STOP)
        with _running_router((200, _SSE, chunks)) as router:
            router.upstream_state.stream_gate.set()
            router.upstream_state.stream_delay = 0.01
            with patch("modules.router._POOL_REQUEST_TIMEOUT", 0.08):
                status, body = _post(router)

        self.assertEqual(status, 200)
        self.assertNotIn(b'"text":"final"', body)
        self.assertIn(b"upstream stream interrupted", body)

    def test_empty_responses_try_the_next_member(self) -> None:
        empties = (
            (_SSE, b""),
            (_SSE, b": ping\n"),
            (_SSE, _START + _STOP),
            ({}, b""),
            ({}, b"{}"),
            ({}, b'{"content":[]}'),
            ({}, b"not json at all"),
        )
        for headers, empty in empties:
            with self.subTest(empty=empty):
                with _running_router(
                    (200, headers, empty),
                    (200, {}, _OK_BODY),
                ) as router:
                    status, body = _post(router)

                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body), json.loads(_OK_BODY))
                self.assertEqual(
                    router.upstream_state.models,
                    ["provider/first", "provider/second"],
                )

    def test_empty_member_is_cooled_down(self) -> None:
        with _running_router((200, {}, b'{"content":[]}'), (200, {}, _OK_BODY)) as router:
            status, _ = _post(router)
            self.assertEqual(status, 200)
            self.assertFalse(router.runtime_state.is_ready("provider/first"))
            self.assertTrue(router.runtime_state.is_ready("provider/second"))

    def test_count_tokens_body_without_content_is_not_empty(self) -> None:
        with _running_router((200, {}, b'{"input_tokens":5}')) as router:
            status, body = _post(router, "/v1/messages/count_tokens")

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"input_tokens": 5})
        self.assertEqual(router.upstream_state.models, ["provider/first"])

    def test_all_members_failing_returns_sanitized_error(self) -> None:
        first_secret = "first-private-value"
        second_secret = "second-private-value"
        with patch("modules.router._POOL_PASSES", 1), _running_router(
            (401, {}, json.dumps({"error": {"message": f"API Key error {first_secret}"}}).encode()),
            (429, {}, json.dumps({"error": {"message": f"Rate limit {second_secret}"}}).encode()),
        ) as router:
            status, body = _post(router)

        error = json.loads(body)["error"]
        self.assertEqual(status, 503)
        self.assertEqual(error["type"], "pool_exhausted")
        self.assertEqual(len(error["attempts"]), 2)
        self.assertNotIn(first_secret, body.decode())
        self.assertNotIn(second_secret, body.decode())

    def test_request_beyond_every_member_cap_is_refused(self) -> None:
        members = [{"model": "provider/first", "rpm": 1},
                   {"model": "provider/second", "rpm": 1}]
        with _running_router(
            (200, {}, _OK_BODY),
            (200, {}, _OK_BODY),
            members=members,
        ) as router:
            served = [_post(router) for _ in range(2)]
            refused = _post(router)

        self.assertEqual([status for status, _ in served], [200, 200])
        self.assertEqual(refused[0], 503)
        self.assertEqual(json.loads(refused[1])["error"]["type"], "pool_exhausted")
        self.assertCountEqual(
            router.upstream_state.models, ["provider/first", "provider/second"])

    def test_second_sweep_recovers_a_member_that_failed_the_first(self) -> None:
        with _running_router(
            (429, {}, b'{"error":{"message":"at capacity"}}'),
            (429, {}, b'{"error":{"message":"at capacity"}}'),
            (200, {}, _OK_BODY),
        ) as router:
            status, body = _post(router)

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), json.loads(_OK_BODY))
        self.assertEqual(
            router.upstream_state.models,
            ["provider/first", "provider/second", "provider/first"],
        )

    def test_pool_gives_up_after_the_configured_number_of_sweeps(self) -> None:
        with patch("modules.router._POOL_PASSES", 3), _running_router(
            *[(500, {}, b"down")] * 6
        ) as router:
            status, body = _post(router)

        self.assertEqual(status, 503)
        self.assertEqual(len(json.loads(body)["error"]["attempts"]), 6)
        self.assertEqual(router.upstream_state.models, ["provider/first", "provider/second"] * 3)

    def test_round_robin_alternates_members(self) -> None:
        with _running_router(
            (200, {}, _OK_BODY),
            (200, {}, _OK_BODY),
            (200, {}, _OK_BODY),
            (200, {}, _OK_BODY),
            strategy="round-robin",
        ) as router:
            for _ in range(4):
                status, _ = _post(router)
                self.assertEqual(status, 200)
        self.assertEqual(
            router.upstream_state.models,
            ["provider/first", "provider/second", "provider/first", "provider/second"],
        )

    def test_non_pool_400_is_forwarded_unchanged(self) -> None:
        error_body = b'{"error":{"message":"direct model rejected request"}}'
        with _running_router((400, {}, error_body)) as router:
            status, body = _post(router, model="provider/direct")

        self.assertEqual(status, 400)
        self.assertEqual(body, error_body)
        self.assertEqual(router.upstream_state.models, ["provider/direct"])

    def test_paced_member_429_uses_short_cooldown(self) -> None:
        members = [
            {"model": "provider/first", "priority": 1, "rpm": 40},
            {"model": "provider/second", "priority": 2},
        ]
        with _running_router(
            (429, {}, b'{"error":{"message":"Rate limit exceeded"}}'),
            (200, {}, _OK_BODY),
            members=members,
        ) as router:
            first_status, _ = _post(router)
            self.assertEqual(first_status, 200)
            self.assertEqual(router.upstream_state.models, ["provider/first", "provider/second"])

            self.assertFalse(router.runtime_state.is_ready("provider/first"))

            with patch("modules.router._now", return_value=time.monotonic() + 30.0):
                self.assertTrue(router.runtime_state.is_ready("provider/first"))

    def test_provider_retry_after_overrides_paced_cooldown(self) -> None:
        members = [
            {"model": "provider/first", "priority": 1, "rpm": 40},
            {"model": "provider/second", "priority": 2},
        ]
        with _running_router(
            (429, {"Retry-After": "2"}, b'{"error":{"message":"Rate limit"}}'),
            (200, {}, _OK_BODY),
            members=members,
        ) as router:
            status, _ = _post(router)
            self.assertEqual(status, 200)
            self.assertFalse(router.runtime_state.is_ready("provider/first"))
            with patch("modules.router._now", return_value=time.monotonic() + 1.0):
                self.assertFalse(router.runtime_state.is_ready("provider/first"))


_R_CREATED = b'event: response.created\ndata: {"type":"response.created"}\n\n'
_R_TEXT = b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"hi"}\n\n'
_R_ITEM = (b'event: response.output_item.added\n'
           b'data: {"type":"response.output_item.added","item":{"type":"function_call"}}\n\n')
_R_DONE = b'event: response.completed\ndata: {"type":"response.completed"}\n\n'
_R_BODY = b'{"object":"response","output":[{"type":"message","content":[{"text":"ok"}]}]}'

_C_TEXT = b'data: {"object":"chat.completion.chunk","choices":[{"delta":{"content":"hi"}}]}\n\n'
_C_TOOL = (b'data: {"object":"chat.completion.chunk","choices":[{"delta":{"tool_calls":'
           b'[{"index":0,"function":{"name":"f"}}]}}]}\n\n')
_C_HOLLOW = b'data: {"object":"chat.completion.chunk","choices":[{"delta":{"role":"assistant"}}]}\n\n'
_C_DONE = b'data: [DONE]\n\n'
_C_BODY = b'{"object":"chat.completion","choices":[{"message":{"role":"assistant","content":"ok"}}]}'


class MultiFormatPoolTests(unittest.TestCase):
    def _one_member(self, path: str, response, expected: bytes) -> None:
        with _running_router((200, *response)) as router:
            status, body = _post(router, path)
        self.assertEqual(status, 200)
        self.assertEqual(body, expected)
        self.assertEqual(router.upstream_state.models, ["provider/first"])

    def _fails_over(self, path: str, bad, good, expected: bytes) -> None:
        with _running_router((200, *bad), (200, *good)) as router:
            status, body = _post(router, path)
        self.assertEqual(status, 200)
        self.assertEqual(body, expected)
        self.assertEqual(router.upstream_state.models, ["provider/first", "provider/second"])

    def test_responses_content_stream_is_not_mistaken_for_empty(self) -> None:
        stream = _R_CREATED + _R_TEXT + _R_DONE
        self._one_member("/v1/responses", (_SSE, stream), stream)

    def test_responses_tool_call_only_stream_counts_as_content(self) -> None:
        stream = _R_CREATED + _R_ITEM + _R_DONE
        self._one_member("/v1/responses", (_SSE, stream), stream)

    def test_responses_empty_stream_tries_next_member(self) -> None:
        good = _R_CREATED + _R_TEXT + _R_DONE
        self._fails_over("/v1/responses", (_SSE, _R_CREATED + _R_DONE), (_SSE, good), good)

    def test_responses_failed_event_tries_next_member(self) -> None:
        failed = b'event: response.failed\ndata: {"type":"response.failed"}\n\n'
        good = _R_CREATED + _R_TEXT + _R_DONE
        self._fails_over("/v1/responses", (_SSE, _R_CREATED + failed), (_SSE, good), good)

    def test_responses_body_without_output_tries_next_member(self) -> None:
        self._fails_over("/v1/responses", ({}, b'{"output":[]}'), ({}, _R_BODY), _R_BODY)

    def test_chat_content_stream_is_not_mistaken_for_empty(self) -> None:
        stream = _C_TEXT + _C_DONE
        self._one_member("/v1/chat/completions", (_SSE, stream), stream)

    def test_chat_tool_call_only_stream_counts_as_content(self) -> None:
        stream = _C_TOOL + _C_DONE
        self._one_member("/v1/chat/completions", (_SSE, stream), stream)

    def test_chat_hollow_stream_tries_next_member(self) -> None:
        good = _C_TEXT + _C_DONE
        self._fails_over("/v1/chat/completions", (_SSE, _C_HOLLOW + _C_DONE), (_SSE, good), good)

    def test_chat_body_without_choices_tries_next_member(self) -> None:
        self._fails_over("/v1/chat/completions", ({}, b'{"choices":[]}'), ({}, _C_BODY), _C_BODY)

    def test_pooled_paths_rewrite_the_model_and_keep_the_path(self) -> None:
        for path, response in (("/v1/responses", (_SSE, _R_CREATED + _R_TEXT + _R_DONE)),
                               ("/v1/chat/completions", (_SSE, _C_TEXT + _C_DONE))):
            with self.subTest(path=path):
                with _running_router((200, *response)) as router:
                    self.assertEqual(_post(router, path)[0], 200)
                self.assertEqual(router.upstream_state.paths, [path])
                self.assertEqual(router.upstream_state.models, ["provider/first"])


if __name__ == "__main__":
    unittest.main()
