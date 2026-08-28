from __future__ import annotations

import http.client
import json
import tempfile
import threading
import time
import unittest
from collections import deque
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from modules.router import (
    _CooldownTable,
    _InFlight,
    _Member,
    _Pool,
    _PoolRegistry,
    _RateLimiter,
    _Rotation,
    _RouterServer,
    _parse_pools,
    _pick_member,
    _retry_after_seconds,
    _rewrite_model,
    _upstream_headers,
)


_SSE = {"Content-Type": "text/event-stream"}
_OK_BODY = b'{"type":"message","role":"assistant","content":[{"type":"text","text":"ok"}]}'
_START = b'event: message_start\ndata: {"type":"message_start"}\n\n'
_DELTA = b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}\n\n'
_STOP = b'event: message_stop\ndata: {"type":"message_stop"}\n\n'


def _rotate(pool: _Pool, cooldowns: _CooldownTable, rotation: _Rotation,
            exclude: set[str] | None = None) -> _Member:
    """One round-robin dispatch: read the cursor, pick, then park it."""
    size = len(pool.members)
    member = _pick_member(pool, cooldowns, exclude or set(), None, rotation.start(pool.name, size))
    rotation.settle(pool.name, size, next(
        index for index, m in enumerate(pool.members) if m.model == member.model))
    return member


def _mk_pool(*members: tuple[str, int, int], strategy: str = "fill-first") -> _Pool:
    """members: (model, rpm, priority) triples."""
    return _Pool(
        name="p",
        members=tuple(_Member(model=m, rpm=r, priority=p) for m, r, p in members),
        strategy=strategy,
    )


class _UpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    responses: deque[tuple[int, dict[str, str], bytes | tuple[bytes, ...]]] = deque()
    models: list[str] = []
    paths: list[str] = []
    stream_gate = threading.Event()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        type(self).models.append(payload["model"])
        type(self).paths.append(self.path)
        status, headers, body = type(self).responses.popleft()
        if status == 0:
            self.close_connection = True
            self.connection.close()
            return
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
            return
        self.wfile.write(body[0])
        self.wfile.flush()
        type(self).stream_gate.wait(timeout=3)
        for chunk in body[1:]:
            if chunk == b"__drop__":
                # A declared Content-Length plus an early close makes the
                # client observe a real mid-body read failure.
                self.connection.close()
                return
            self.wfile.write(chunk)
            self.wfile.flush()


@contextmanager
def _running_router(
    *responses: tuple[int, dict[str, str], bytes | tuple[bytes, ...]],
    members: list[dict[str, object]] | None = None,
    strategy: str = "fill-first",
):
    _UpstreamHandler.responses = deque(responses)
    _UpstreamHandler.models = []
    _UpstreamHandler.paths = []
    _UpstreamHandler.stream_gate = threading.Event()
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _UpstreamHandler)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
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
            router = _RouterServer(("127.0.0.1", 0))
            router.pools = _PoolRegistry(pools_file)
            router_thread = threading.Thread(target=router.serve_forever, daemon=True)
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
    connection = http.client.HTTPConnection("127.0.0.1", router.server_port, timeout=3)
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
        self.assertEqual(members[0].rpm, 1)      # default rpm
        self.assertEqual(members[0].priority, 0)  # default priority
        self.assertEqual(members[1].rpm, 5)
        self.assertEqual(members[1].priority, 2)

    def test_disabled_pools_skipped(self) -> None:
        payload = {"pools": [{"name": "off", "enabled": False, "members": [{"model": "a/x"}]}]}
        self.assertEqual(_parse_pools(payload), {})

    def test_strategy_parsed_with_fallback(self) -> None:
        good = {"pools": [{"name": "p", "members": [{"model": "a/x"}], "strategy": "round-robin"}]}
        self.assertEqual(_parse_pools(good)["p"].strategy, "round-robin")
        bad = {"pools": [{"name": "p", "members": [{"model": "a/x"}], "strategy": "chaos"}]}
        self.assertEqual(_parse_pools(bad)["p"].strategy, "fill-first")


class PickMemberTests(unittest.TestCase):
    def test_strict_priority_tier(self) -> None:
        pool = _mk_pool(("a", 100, 2), ("b", 1, 1))
        cd = _CooldownTable()
        # priority 1 beats priority 2 regardless of rpm
        for _ in range(20):
            self.assertEqual(_pick_member(pool, cd, exclude=set()).model, "b")

    def test_falls_to_next_tier_when_top_excluded(self) -> None:
        pool = _mk_pool(("a", 1, 1), ("b", 1, 2))
        cd = _CooldownTable()
        self.assertEqual(_pick_member(pool, cd, exclude={"a"}).model, "b")

    def test_falls_to_next_tier_when_top_in_cooldown(self) -> None:
        pool = _mk_pool(("a", 1, 1), ("b", 1, 2))
        cd = _CooldownTable()
        cd.cooldown("a", 30, "test")
        # A ready alternative is preferred before a last-resort cooling retry.
        self.assertEqual(_pick_member(pool, cd, exclude=set()).model, "b")

    def test_all_cooling_remains_selectable(self) -> None:
        pool = _mk_pool(("a", 1, 1))
        cd = _CooldownTable()
        cd.cooldown("a", 30, "test")
        self.assertEqual(_pick_member(pool, cd, exclude=set()).model, "a")

    def test_rpm_weight_within_tier(self) -> None:
        # 999-vs-1 rpm; picking 400 times should heavily favour the 999 member
        pool = _mk_pool(("heavy", 999, 0), ("light", 1, 0))
        cd = _CooldownTable()
        counts = {"heavy": 0, "light": 0}
        for _ in range(400):
            m = _pick_member(pool, cd, exclude=set())
            counts[m.model] += 1
        self.assertGreater(counts["heavy"], counts["light"] * 20)


class RoundRobinTests(unittest.TestCase):
    def test_rotates_evenly_ignoring_tiers_and_weights(self) -> None:
        pool = _mk_pool(("a", 1, 2), ("b", 999, 1), ("c", 1, 0), strategy="round-robin")
        cd, rotation = _CooldownTable(), _Rotation()
        picks = [_rotate(pool, cd, rotation).model for _ in range(6)]
        self.assertEqual(picks, ["a", "b", "c", "a", "b", "c"])

    def test_skips_cooling_member_in_rotation(self) -> None:
        pool = _mk_pool(("a", 1, 0), ("b", 1, 0), strategy="round-robin")
        cd, rotation = _CooldownTable(), _Rotation()
        cd.cooldown("a", 30, "test")
        picks = [_rotate(pool, cd, rotation).model for _ in range(3)]
        self.assertEqual(picks, ["b", "b", "b"])

    def test_all_cooling_still_rotates(self) -> None:
        pool = _mk_pool(("a", 1, 0), ("b", 1, 0), strategy="round-robin")
        cd, rotation = _CooldownTable(), _Rotation()
        cd.cooldown("a", 30, "test")
        cd.cooldown("b", 30, "test")
        picks = [_rotate(pool, cd, rotation).model for _ in range(4)]
        self.assertEqual(picks, ["a", "b", "a", "b"])

    def test_retry_does_not_replay_on_the_next_request(self) -> None:
        # The old cursor modulo'd a shrinking candidate list, so the member that
        # served a retry was handed the very next request as well.
        pool = _mk_pool(("a", 1, 0), ("b", 1, 0), ("c", 1, 0), strategy="round-robin")
        cd, rotation = _CooldownTable(), _Rotation()
        first = _rotate(pool, cd, rotation)
        retry = _rotate(pool, cd, rotation, exclude={first.model})
        following = [_rotate(pool, cd, rotation).model for _ in range(3)]
        self.assertEqual([first.model, retry.model], ["a", "b"])
        self.assertEqual(following, ["c", "a", "b"])

    def test_cooling_member_reclaims_its_own_slot(self) -> None:
        pool = _mk_pool(("a", 1, 0), ("b", 1, 0), ("c", 1, 0), strategy="round-robin")
        cd, rotation = _CooldownTable(), _Rotation()
        cd.cooldown("b", 30, "test")
        self.assertEqual([_rotate(pool, cd, rotation).model for _ in range(4)],
                         ["a", "c", "a", "c"])
        cd.clear("b")
        self.assertEqual([_rotate(pool, cd, rotation).model for _ in range(3)],
                         ["a", "b", "c"])

    def test_cursor_resets_when_member_count_changes(self) -> None:
        rotation = _Rotation()
        rotation.settle("p", 3, 2)
        self.assertEqual(rotation.start("p", 3), 0)
        rotation.settle("p", 3, 1)
        self.assertEqual(rotation.start("p", 2), 0)


def _mk_pool_limited(*members: tuple[str, int, int, int | None]) -> _Pool:
    """members: (model, rpm, priority, limit) — limit may be None."""
    return _Pool(
        name="p",
        members=tuple(
            _Member(model=m, rpm=r, priority=p, limit=lim)
            for m, r, p, lim in members
        ),
    )


class PacingTests(unittest.TestCase):
    def test_unpaced_member_is_not_limited(self) -> None:
        pool = _mk_pool_limited(("a", 1, 1, None), ("b", 1, 2, None))
        cd = _CooldownTable()
        lim = _RateLimiter()
        for _ in range(150):
            m = _pick_member(pool, cd, exclude=set(), limiter=lim)
            lim.record(m.model, m.limit)
        # top priority always chosen, never paced out
        self.assertEqual(cd.is_ready("a"), True)
        self.assertEqual(
            _pick_member(pool, cd, exclude=set(), limiter=lim).model, "a"
        )

    def test_paced_top_member_spills_to_next_tier(self) -> None:
        pool = _mk_pool_limited(("fast", 1, 1, 2), ("slow", 1, 2, None))
        cd = _CooldownTable()
        lim = _RateLimiter()
        chosen: list[str] = []
        # two dispatches fit within the limit; subsequent ones must spill
        for _ in range(6):
            m = _pick_member(pool, cd, exclude=set(), limiter=lim)
            chosen.append(m.model)
            lim.record(m.model, m.limit)
        self.assertEqual(chosen[:2], ["fast", "fast"])
        # after the limit is hit, traffic spills to the lower-priority member
        self.assertIn("slow", chosen[2:])
        self.assertNotIn("fast", chosen[2:])

    def test_all_paced_out_falls_back_to_top_priority(self) -> None:
        # if every member is paced out, the router must still pick someone
        # rather than fail the request; the highest-priority one wins.
        pool = _mk_pool_limited(("a", 1, 1, 1), ("b", 1, 2, 1))
        cd = _CooldownTable()
        lim = _RateLimiter()
        lim.record("a", 1)
        lim.record("b", 1)
        self.assertEqual(
            _pick_member(pool, cd, exclude=set(), limiter=lim).model, "a"
        )

    def test_window_expiry_restores_capacity(self) -> None:
        pool = _mk_pool_limited(("fast", 1, 1, 1), ("slow", 1, 2, None))
        cd = _CooldownTable()
        lim = _RateLimiter()
        # exhaust the fast member's lone slot
        lim.record("fast", 1)
        # fast is paced out, so traffic spills to the lower-priority member
        self.assertEqual(
            _pick_member(pool, cd, exclude=set(), limiter=lim).model, "slow"
        )
        # after the 60s window passes, the fast member regains capacity
        # (relative to the live clock: a fixed epoch breaks on long uptimes)
        with patch("modules.router.time.monotonic", return_value=time.monotonic() + 120.0):
            self.assertEqual(
                _pick_member(pool, cd, exclude=set(), limiter=lim).model, "fast"
            )


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
        rewritten = _rewrite_model(body, parsed, "real/model")
        out = json.loads(rewritten)
        self.assertEqual(out["model"], "real/model")
        self.assertIn("my-pool", out["messages"][0]["content"])  # content untouched


class HeaderRewriteTests(unittest.TestCase):
    def test_hop_and_auth_are_stripped_before_forwarding(self) -> None:
        # simulate BaseHTTPRequestHandler headers as a dict-like
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
        # Host must point at CLIProxyAPI, not our router
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
    def test_auth_error_tries_next_member(self) -> None:
        for status_code in (401, 403):
            with self.subTest(status=status_code):
                with _running_router(
                    (status_code, {}, b'{"error":{"message":"API Key error"}}'),
                    (200, {}, _OK_BODY),
                ) as router:
                    status, body = _post(router)

                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body), json.loads(_OK_BODY))
                self.assertEqual(
                    _UpstreamHandler.models,
                    ["provider/first", "provider/second"],
                )

    def test_api_key_error_in_bad_request_tries_next_member(self) -> None:
        with _running_router(
            (400, {}, b'{"error":{"message":"Invalid API key supplied"}}'),
            (200, {}, _OK_BODY),
        ) as router:
            status, body = _post(router)

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), json.loads(_OK_BODY))
        self.assertEqual(_UpstreamHandler.models, ["provider/first", "provider/second"])

    def test_malformed_provider_json_in_bad_request_tries_next_member(self) -> None:
        malformed_responses = (
            b'{"error":{"message":"NVIDIA NIM returned malformed JSON in tool call"}}',
            b'{"error":{"message":"Failed to parse assistant response"}}',
            (
                b'{"error":{"message":"Failed to deserialize the JSON body into the target '
                b'type: data did not match any variant of untagged enum '
                b'ChatCompletionRequestToolMessageContent at line 1 column 1234711"}}'
            ),
        )
        for error_body in malformed_responses:
            with self.subTest(error_body=error_body):
                with _running_router(
                    (400, {}, error_body),
                    (200, {}, _OK_BODY),
                ) as router:
                    status, body = _post(router)

                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body), json.loads(_OK_BODY))
                self.assertEqual(
                    _UpstreamHandler.models,
                    ["provider/first", "provider/second"],
                )

    def test_stream_error_event_tries_next_member_before_forwarding(self) -> None:
        stream_errors = (
            b'event: error\ndata: {"type":"error","error":{"message":"API Key error"}}\n\n',
            b'event: error\ndata: {"type":"error","error":{"message":"unknown provider failure"}}\n\n',
        )
        for stream_error in stream_errors:
            with self.subTest(stream_error=stream_error):
                with _running_router(
                    (200, {"Content-Type": "text/event-stream"}, stream_error),
                    (200, {}, _OK_BODY),
                ) as router:
                    status, body = _post(router)

                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body), json.loads(_OK_BODY))
                self.assertEqual(
                    _UpstreamHandler.models,
                    ["provider/first", "provider/second"],
                )

    def test_server_error_tries_next_member(self) -> None:
        with _running_router(
            (500, {}, b'{"error":{"message":"provider failed"}}'),
            (200, {}, _OK_BODY),
        ) as router:
            status, body = _post(router)

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), json.loads(_OK_BODY))
        self.assertEqual(_UpstreamHandler.models, ["provider/first", "provider/second"])

    def test_transport_error_tries_next_member(self) -> None:
        with _running_router(
            (0, {}, b""),
            (200, {}, _OK_BODY),
        ) as router:
            status, body = _post(router)

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), json.loads(_OK_BODY))
        self.assertEqual(_UpstreamHandler.models, ["provider/first", "provider/second"])

    def test_count_tokens_rate_limit_tries_next_member(self) -> None:
        with _running_router(
            (429, {"Retry-After": "1"}, b'{"error":{"message":"Rate limit exceeded"}}'),
            (200, {}, b'{"input_tokens":7}'),
        ) as router:
            status, body = _post(router, "/v1/messages/count_tokens")

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"input_tokens": 7})
        self.assertEqual(_UpstreamHandler.models, ["provider/first", "provider/second"])
        self.assertEqual(
            _UpstreamHandler.paths,
            ["/v1/messages/count_tokens", "/v1/messages/count_tokens"],
        )

    def test_successful_stream_is_forwarded_incrementally(self) -> None:
        # The router commits on the first content frame, so the head it buffered
        # must reach the client before the rest of the stream is produced.
        with _running_router(
            (200, _SSE, (_START + _DELTA, _STOP)),
        ) as router:
            body = json.dumps({"model": "test-pool", "messages": []}).encode("utf-8")
            connection = http.client.HTTPConnection("127.0.0.1", router.server_port, timeout=3)
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
            _UpstreamHandler.stream_gate.set()
            rest = response.read()
            connection.close()

        self.assertEqual(response.status, 200)
        self.assertEqual(first, _START + _DELTA)
        self.assertEqual(rest, _STOP)
        self.assertEqual(_UpstreamHandler.models, ["provider/first"])

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
                    _UpstreamHandler.models,
                    ["provider/first", "provider/second"],
                )

    def test_empty_member_is_cooled_down(self) -> None:
        with _running_router((200, {}, b'{"content":[]}'), (200, {}, _OK_BODY)) as router:
            status, _ = _post(router)
            self.assertEqual(status, 200)
            self.assertFalse(router.cooldowns.is_ready("provider/first"))
            self.assertTrue(router.cooldowns.is_ready("provider/second"))

    def test_count_tokens_body_without_content_is_not_empty(self) -> None:
        with _running_router((200, {}, b'{"input_tokens":5}')) as router:
            status, body = _post(router, "/v1/messages/count_tokens")

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"input_tokens": 5})
        self.assertEqual(_UpstreamHandler.models, ["provider/first"])

    def test_stream_dying_before_content_tries_the_next_member(self) -> None:
        with _running_router(
            (200, {**_SSE, "Content-Length": "999"}, (_START, b"__drop__")),
            (200, {}, _OK_BODY),
        ) as router:
            _UpstreamHandler.stream_gate.set()
            status, body = _post(router)

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), json.loads(_OK_BODY))
        self.assertEqual(_UpstreamHandler.models, ["provider/first", "provider/second"])

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

    def test_any_member_4xx_tries_next_member_without_inspecting_text(self) -> None:
        for status_code in (400, 404, 422):
            with self.subTest(status=status_code):
                with _running_router(
                    (status_code, {}, b'{"error":{"message":"arbitrary rejection"}}'),
                    (200, {}, _OK_BODY),
                ) as router:
                    status, body = _post(router)

                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body), json.loads(_OK_BODY))
                self.assertEqual(
                    _UpstreamHandler.models,
                    ["provider/first", "provider/second"],
                )

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
            _UpstreamHandler.models,
            ["provider/first", "provider/second", "provider/first"],
        )

    def test_pool_gives_up_after_the_configured_number_of_sweeps(self) -> None:
        with patch("modules.router._POOL_PASSES", 3), _running_router(
            *[(500, {}, b"down")] * 6
        ) as router:
            status, body = _post(router)

        self.assertEqual(status, 503)
        self.assertEqual(len(json.loads(body)["error"]["attempts"]), 6)
        self.assertEqual(_UpstreamHandler.models, ["provider/first", "provider/second"] * 3)

    def test_round_robin_alternates_members(self) -> None:
        # Same default members (priority 1 / 2): fill-first would send every
        # request to provider/first; round-robin must alternate instead.
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
            _UpstreamHandler.models,
            ["provider/first", "provider/second", "provider/first", "provider/second"],
        )

    def test_non_pool_400_is_forwarded_unchanged(self) -> None:
        error_body = b'{"error":{"message":"direct model rejected request"}}'
        with _running_router((400, {}, error_body)) as router:
            status, body = _post(router, model="provider/direct")

        self.assertEqual(status, 400)
        self.assertEqual(body, error_body)
        self.assertEqual(_UpstreamHandler.models, ["provider/direct"])

    def test_paced_member_429_uses_short_cooldown(self) -> None:
        # A member with an rpm cap should cool for the short paced-429 value,
        # not the global 60s default, so it comes back within the same minute.
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
            self.assertEqual(_UpstreamHandler.models, ["provider/first", "provider/second"])

            # the paced member was put into a short cooldown on the 429
            self.assertFalse(router.cooldowns.is_ready("provider/first"))

            with patch("modules.router.time.monotonic", return_value=time.monotonic() + 120.0):
                # after the short cooldown elapses it must be eligible again
                self.assertTrue(router.cooldowns.is_ready("provider/first"))

    def test_provider_retry_after_overrides_paced_cooldown(self) -> None:
        # If the provider sends its own Retry-After, that wins over the paced
        # default — we trust the server's knowledge of its own window.
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
            self.assertFalse(router.cooldowns.is_ready("provider/first"))
            # provider said 2s, so it stays cooled at t+1
            with patch("modules.router.time.monotonic", return_value=time.monotonic() + 1.0):
                self.assertFalse(router.cooldowns.is_ready("provider/first"))


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
    """Pooling must judge /v1/responses and /v1/chat/completions in their own grammar."""

    def _one_member(self, path: str, response, expected: bytes) -> None:
        with _running_router((200, *response)) as router:
            status, body = _post(router, path)
        self.assertEqual(status, 200)
        self.assertEqual(body, expected)
        self.assertEqual(_UpstreamHandler.models, ["provider/first"])

    def _fails_over(self, path: str, bad, good, expected: bytes) -> None:
        with _running_router((200, *bad), (200, *good)) as router:
            status, body = _post(router, path)
        self.assertEqual(status, 200)
        self.assertEqual(body, expected)
        self.assertEqual(_UpstreamHandler.models, ["provider/first", "provider/second"])

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
                self.assertEqual(_UpstreamHandler.paths, [path])
                self.assertEqual(_UpstreamHandler.models, ["provider/first"])


class WeightedStrategyTests(unittest.TestCase):
    def test_ignores_priority_tiers(self) -> None:
        pool = _mk_pool(("a", 1, 0), ("b", 1, 9), strategy="weighted")
        picks = {_pick_member(pool, _CooldownTable(), set()).model for _ in range(60)}
        self.assertEqual(picks, {"a", "b"})

    def test_rpm_biases_the_draw(self) -> None:
        pool = _mk_pool(("heavy", 99, 0), ("light", 1, 0), strategy="weighted")
        cooldowns = _CooldownTable()
        picks = [_pick_member(pool, cooldowns, set()).model for _ in range(200)]
        self.assertGreater(picks.count("heavy"), picks.count("light"))

    def test_parsed_as_a_valid_strategy(self) -> None:
        payload = {"pools": [{"name": "p", "members": [{"model": "a/x"}], "strategy": "weighted"}]}
        self.assertEqual(_parse_pools(payload)["p"].strategy, "weighted")


class LeastBusyStrategyTests(unittest.TestCase):
    def test_prefers_the_idle_member(self) -> None:
        pool = _mk_pool(("busy", 1, 0), ("idle", 1, 0), strategy="least-busy")
        inflight = _InFlight()
        with inflight.hold("busy"):
            picks = {_pick_member(pool, _CooldownTable(), set(), inflight=inflight).model
                     for _ in range(30)}
        self.assertEqual(picks, {"idle"})

    def test_ignores_priority_tiers_when_the_top_tier_is_loaded(self) -> None:
        pool = _mk_pool(("top", 1, 0), ("low", 1, 9), strategy="least-busy")
        inflight = _InFlight()
        with inflight.hold("top"):
            self.assertEqual(
                _pick_member(pool, _CooldownTable(), set(), inflight=inflight).model, "low")

    def test_all_idle_spreads_across_members(self) -> None:
        pool = _mk_pool(("a", 1, 0), ("b", 1, 0), strategy="least-busy")
        inflight = _InFlight()
        picks = {_pick_member(pool, _CooldownTable(), set(), inflight=inflight).model
                 for _ in range(60)}
        self.assertEqual(picks, {"a", "b"})

    def test_hold_releases_the_slot(self) -> None:
        inflight = _InFlight()
        with inflight.hold("m"):
            self.assertEqual(inflight.count("m"), 1)
        self.assertEqual(inflight.count("m"), 0)

    def test_hold_releases_on_exception(self) -> None:
        inflight = _InFlight()
        with self.assertRaises(RuntimeError), inflight.hold("m"):
            raise RuntimeError("boom")
        self.assertEqual(inflight.count("m"), 0)

    def test_dispatch_releases_the_slot_after_the_response(self) -> None:
        with _running_router((200, {}, _OK_BODY)) as router:
            self.assertEqual(_post(router)[0], 200)
            # The handler thread unwinds the hold after the client has its body.
            deadline = time.monotonic() + 5
            while router.inflight.count("provider/first") and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(router.inflight.count("provider/first"), 0)


if __name__ == "__main__":
    unittest.main()
