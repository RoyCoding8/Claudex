from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from collections import deque
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from modules.router import (
    _CooldownTable,
    _Member,
    _Pool,
    _PoolRegistry,
    _RouterServer,
    _parse_pools,
    _pick_member,
    _retry_after_seconds,
    _rewrite_model,
    _upstream_headers,
)


def _mk_pool(*members: tuple[str, int, int]) -> _Pool:
    """members: (model, rpm, priority) triples."""
    return _Pool(
        name="p",
        members=tuple(_Member(model=m, rpm=r, priority=p) for m, r, p in members),
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
            self.wfile.write(chunk)
            self.wfile.flush()


@contextmanager
def _running_router(
    *responses: tuple[int, dict[str, str], bytes | tuple[bytes, ...]],
):
    _UpstreamHandler.responses = deque(responses)
    _UpstreamHandler.models = []
    _UpstreamHandler.paths = []
    _UpstreamHandler.stream_gate = threading.Event()
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _UpstreamHandler)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()

    with tempfile.TemporaryDirectory() as temporary:
        pools_file = Path(temporary) / "pools.json"
        pools_file.write_text(
            json.dumps(
                {
                    "version": 1,
                    "pools": [
                        {
                            "name": "test-pool",
                            "members": [
                                {"model": "provider/first", "priority": 1},
                                {"model": "provider/second", "priority": 2},
                            ],
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


def _post(router: _RouterServer, path: str = "/v1/messages") -> tuple[int, bytes]:
    body = json.dumps({"model": "test-pool", "messages": []}).encode("utf-8")
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
        self.assertEqual(_pick_member(pool, cd, exclude=set()).model, "b")

    def test_returns_none_when_all_cooling_and_excluded(self) -> None:
        pool = _mk_pool(("a", 1, 1))
        cd = _CooldownTable()
        cd.cooldown("a", 30, "test")
        self.assertIsNone(_pick_member(pool, cd, exclude=set()))

    def test_rpm_weight_within_tier(self) -> None:
        # 999-vs-1 rpm; picking 400 times should heavily favour the 999 member
        pool = _mk_pool(("heavy", 999, 0), ("light", 1, 0))
        cd = _CooldownTable()
        counts = {"heavy": 0, "light": 0}
        for _ in range(400):
            m = _pick_member(pool, cd, exclude=set())
            counts[m.model] += 1
        self.assertGreater(counts["heavy"], counts["light"] * 20)


class BodyRewriteTests(unittest.TestCase):
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
                    (200, {}, b'{"ok":true}'),
                ) as router:
                    status, body = _post(router)

                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body), {"ok": True})
                self.assertEqual(
                    _UpstreamHandler.models,
                    ["provider/first", "provider/second"],
                )

    def test_api_key_error_in_bad_request_tries_next_member(self) -> None:
        with _running_router(
            (400, {}, b'{"error":{"message":"Invalid API key supplied"}}'),
            (200, {}, b'{"ok":true}'),
        ) as router:
            status, body = _post(router)

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True})
        self.assertEqual(_UpstreamHandler.models, ["provider/first", "provider/second"])

    def test_stream_error_event_tries_next_member_before_forwarding(self) -> None:
        with _running_router(
            (
                200,
                {"Content-Type": "text/event-stream"},
                b'event: error\ndata: {"type":"error","error":{"message":"API Key error"}}\n\n',
            ),
            (200, {}, b'{"ok":true}'),
        ) as router:
            status, body = _post(router)

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True})
        self.assertEqual(_UpstreamHandler.models, ["provider/first", "provider/second"])

    def test_server_error_tries_next_member(self) -> None:
        with _running_router(
            (500, {}, b'{"error":{"message":"provider failed"}}'),
            (200, {}, b'{"ok":true}'),
        ) as router:
            status, body = _post(router)

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True})
        self.assertEqual(_UpstreamHandler.models, ["provider/first", "provider/second"])

    def test_transport_error_tries_next_member(self) -> None:
        with _running_router(
            (0, {}, b""),
            (200, {}, b'{"ok":true}'),
        ) as router:
            status, body = _post(router)

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True})
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
        with _running_router(
            (
                200,
                {"Content-Type": "text/event-stream"},
                (b"event: first\n\n", b"event: second\n\n"),
            ),
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
            first = response.read(14)
            _UpstreamHandler.stream_gate.set()
            rest = response.read()
            connection.close()

        self.assertEqual(response.status, 200)
        self.assertEqual(first, b"event: first\n\n")
        self.assertEqual(rest, b"event: second\n\n")
        self.assertEqual(_UpstreamHandler.models, ["provider/first"])

    def test_all_members_failing_returns_sanitized_error(self) -> None:
        first_secret = "first-private-value"
        second_secret = "second-private-value"
        with _running_router(
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

    def test_validation_error_is_forwarded_without_retry(self) -> None:
        with _running_router(
            (400, {}, b'{"error":{"message":"invalid request schema"}}'),
        ) as router:
            status, body = _post(router)

        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["message"], "invalid request schema")
        self.assertEqual(_UpstreamHandler.models, ["provider/first"])


if __name__ == "__main__":
    unittest.main()
