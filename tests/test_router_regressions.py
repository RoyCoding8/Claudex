"""Regression tests for the deep-reliability audit (tmp/AUDIT.md)."""
from __future__ import annotations

import gzip
import http.client
import json
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from modules.pools import ModelPool, PoolMember, load_pools
from modules.router import (
    _parse_pools,
    _PoolRegistry,
    _RouterHandler,
    _upstream_headers,
    _UpstreamResponse,
)
from tests.test_router import (
    _OK_BODY,
    _SSE,
    _post,
    _running_router,
)


class StatusPhraseTests(unittest.TestCase):
    def test_status_520_reaches_client(self):
        with _running_router((520, {}, b"cloudflare hiccup")) as router:
            status, body = _post(router, "/v1/completions", model="provider/direct")
        self.assertEqual(status, 520)

    def test_status_299_reaches_client(self):
        with _running_router((299, {}, _OK_BODY)) as router:
            status, _ = _post(router)
        self.assertEqual(status, 299)


class EncodingTests(unittest.TestCase):
    def test_gzip_body_is_not_malformed(self):
        with _running_router(
            (200, {"Content-Encoding": "gzip"}, gzip.compress(_OK_BODY)),
            (200, {}, _OK_BODY),
        ) as router:
            status, body = _post(router)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), json.loads(_OK_BODY))

    def test_accept_encoding_not_forwarded(self):
        headers = _upstream_headers({"Accept-Encoding": "gzip, deflate, br", "X-Keep": "1"}, b"{}")
        self.assertEqual(headers.get("Accept-Encoding", "identity"), "identity")
        self.assertEqual(headers["X-Keep"], "1")


_START_FRAME = b'event: message_start\ndata: {"type":"message_start"}\n\n'
_DELTA_FRAME = b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"text":"hi"}}\n\n'
_STOP_FRAME = b'event: message_stop\ndata: {"type":"message_stop"}\n\n'


class _DeadClient:
    def __init__(self, fail_after, error=None):
        self.writes, self.fail_after = 0, fail_after
        self.error = error or BrokenPipeError(32, "The pipe has been ended")
    def write(self, data):
        self.writes += 1
        if self.writes > self.fail_after:
            raise self.error
        return len(data)
    def flush(self): pass


def _bare_handler(wfile):
    handler = _RouterHandler.__new__(_RouterHandler)
    handler.wfile = wfile
    handler.send_response = lambda *a, **k: None
    handler.send_header = lambda *a, **k: None
    handler.end_headers = lambda *a, **k: None
    return handler


class _StubConn:
    def __init__(self): self.closed = 0
    def close(self): self.closed += 1


def _sse_upstream(chunks, conn=None):
    return _UpstreamResponse(200, "OK", [("Content-Type", "text/event-stream")],
                             iter(chunks), conn or _StubConn())


class ClientAbortTests(unittest.TestCase):
    def test_client_abort_does_not_cool_member(self):
        handler = _bare_handler(_DeadClient(fail_after=1))
        upstream = _sse_upstream([_START_FRAME, _DELTA_FRAME, _STOP_FRAME])
        self.assertTrue(handler._stream_upstream(upstream, request_id="t", member="m"))

    def test_upstream_drop_still_cools_member(self):
        def broken_iter():
            yield _START_FRAME
            raise ConnectionResetError(104, "upstream reset")
        handler = _bare_handler(_DeadClient(fail_after=99))
        upstream = _sse_upstream(broken_iter())
        self.assertFalse(handler._stream_upstream(upstream, request_id="t", member="m"))

    def test_client_write_timeout_is_treated_as_disconnect(self):
        handler = _bare_handler(_DeadClient(0, error=TimeoutError("client stopped draining")))
        upstream = _sse_upstream([_START_FRAME, _DELTA_FRAME, _STOP_FRAME])
        self.assertTrue(handler._stream_upstream(upstream, request_id="t", member="m"))

    def test_client_write_timeout_on_error_frame_is_swallowed(self):
        def interrupted():
            yield _START_FRAME
            raise ConnectionResetError(104, "upstream reset")
        handler = _bare_handler(_DeadClient(1, error=TimeoutError("client stopped draining")))
        upstream = _sse_upstream(interrupted())
        self.assertFalse(handler._stream_upstream(upstream, request_id="t", member="m"))

    def test_client_write_timeout_on_buffered_body_is_swallowed(self):
        handler = _bare_handler(_DeadClient(0, error=TimeoutError("client stopped draining")))
        upstream = _UpstreamResponse(200, "OK", [("Content-Type", "application/json")],
                                     iter(()), _StubConn(), buffered=_OK_BODY)
        self.assertTrue(handler._stream_upstream(upstream, request_id="t", member="m"))


class PostCommitTests(unittest.TestCase):
    def test_undrained_client_does_not_retry_after_commit(self):
        big_delta = (b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"text":"'
                     + b"x" * 1_000_000 + b'"}}\n\n')
        with patch.object(_RouterHandler, "timeout", 0.3), _running_router(
            (200, _SSE, (_START_FRAME + _DELTA_FRAME, big_delta)),
        ) as router:
            connection = socket.create_connection(("127.0.0.1", router.server_port), timeout=10)
            body = json.dumps({"model": "test-pool", "messages": []}).encode()
            connection.sendall(
                (f"POST /v1/messages HTTP/1.1\r\nHost: t\r\n"
                 f"Authorization: Bearer test-router-key\r\n"
                 f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
                 f"Connection: close\r\n\r\n").encode() + body)
            head = b""
            while b"\r\n\r\n" not in head:
                head += connection.recv(65536)
            router.upstream_state.stream_gate.set()
            deadline = time.monotonic() + 2.0
            while len(router.upstream_state.models) < 2 and time.monotonic() < deadline:
                time.sleep(0.05)
            connection.close()
        self.assertEqual(router.upstream_state.models, ["provider/first"])
        self.assertTrue(router.cooldowns.is_ready("provider/first"))


class AuthHardeningTests(unittest.TestCase):
    def test_non_ascii_key_is_rejected_not_fatal(self):
        with _running_router((200, {}, _OK_BODY)) as router:
            outcomes = []
            for headers in ({"x-api-key": "é-not-the-key"}, {"Authorization": "Bearer é-not-the-key"}):
                connection = http.client.HTTPConnection("127.0.0.1", router.server_port, timeout=10)
                connection.request("POST", "/v1/messages", body=b"{}", headers=headers)
                response = connection.getresponse()
                outcomes.append(response.status)
                response.read()
                connection.close()
        self.assertEqual(outcomes, [401, 401])


class ConnectionLifetimeTests(unittest.TestCase):
    def test_failed_attempt_closes_upstream(self):
        original = http.client.HTTPConnection
        closable = []

        class Counted(original):
            def close(self):
                closable.append(1)
                super().close()

        with patch("modules.router.HTTPConnection", Counted), _running_router(
            (200, {"Content-Encoding": "gzip"}, gzip.compress(_OK_BODY)),
            (200, {}, _OK_BODY),
        ) as router:
            status, _ = _post(router)
        self.assertEqual(status, 200)
        self.assertTrue(closable)


class PoolRegistryTests(unittest.TestCase):
    def test_non_utf8_pools_file_is_survivable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pools.json"
            path.write_bytes(b'{"pools":[{"name":"p\xff","members":[{"model":"a"}]}]}')
            registry = _PoolRegistry(path)
            self.assertIsNone(registry.get("p"))

    def test_pool_stat_permission_error_is_survivable(self):
        real_stat = Path.stat
        def locked_stat(self, *args, **kwargs):
            if self.suffix == ".json" and self.name == "pools.json":
                raise PermissionError(13, "held by antivirus")
            return real_stat(self, *args, **kwargs)
        with _running_router((200, {}, _OK_BODY)) as router:
            with patch.object(Path, "stat", locked_stat):
                status, _ = _post(router)
        self.assertEqual(status, 200)


class TimeoutTests(unittest.TestCase):
    def test_relax_timeout_respects_deadline(self):
        from modules.router import _UPSTREAM_TIMEOUT
        class FakeSocket:
            def __init__(self): self.timeout = None
            def settimeout(self, value): self.timeout = value
        fake = FakeSocket()
        response = _UpstreamResponse(200, "OK", [], iter(()), _StubConn(), body_socket=fake)
        response.relax_timeout(deadline=time.monotonic() + 5.0)
        self.assertIsNotNone(fake.timeout)
        self.assertLessEqual(fake.timeout, 5.0)
        self.assertLess(fake.timeout, _UPSTREAM_TIMEOUT)

    def test_handler_has_timeout(self):
        self.assertIsNotNone(_RouterHandler.timeout)
        self.assertGreater(_RouterHandler.timeout, 0)


class ChunkedBodyTests(unittest.TestCase):
    def test_chunked_request_body_is_read(self):
        with _running_router((200, {}, _OK_BODY)) as router:
            body = json.dumps({"model": "test-pool", "messages": []}).encode()
            connection = http.client.HTTPConnection("127.0.0.1", router.server_port, timeout=10)
            connection.request("POST", "/v1/messages", body=body, encode_chunked=True,
                               headers={"Authorization": "Bearer test-router-key",
                                        "Transfer-Encoding": "chunked"})
            response = connection.getresponse()
            status = response.status
            response.read()
            connection.close()
        self.assertEqual(status, 200)
        self.assertEqual(router.upstream_state.models, ["provider/first"])


class RetryClassificationTests(unittest.TestCase):
    def test_400_is_returned_not_retried(self):
        with _running_router((400, {}, b'{"error":{"message":"must not be empty"}}')) as router:
            status, body = _post(router)
        self.assertEqual(status, 400)
        self.assertIn(b"must not be empty", body)
        self.assertEqual(router.upstream_state.models, ["provider/first"])
        self.assertTrue(router.cooldowns.is_ready("provider/first"))
        self.assertTrue(router.cooldowns.is_ready("provider/second"))

    def test_500_is_still_retried(self):
        with _running_router((500, {}, b"down"), (200, {}, _OK_BODY)) as router:
            status, _ = _post(router)
        self.assertEqual(status, 200)
        self.assertEqual(router.upstream_state.models, ["provider/first", "provider/second"])


class ParserAgreementTests(unittest.TestCase):
    DOCUMENTS = {
        "duplicate_members": {"pools": [{"name": "p", "members": [{"model": "a"}, {"model": "a", "rpm": 9}, {"model": "b"}]}]},
        "unknown_strategy": {"pools": [{"name": "p", "members": [{"model": "a"}], "strategy": "sticky"}]},
        "string_member": {"pools": [{"name": "p", "members": ["a", {"model": "b"}]}]},
        "zero_rpm": {"pools": [{"name": "p", "members": [{"model": "a", "rpm": 0}]}]},
    }

    def tearDown(self) -> None:
        from modules.pools import _LOADED_DIGESTS
        _LOADED_DIGESTS.clear()

    def test_router_and_loader_agree(self):
        for label, document in self.DOCUMENTS.items():
            with self.subTest(document=label):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "pools.json"
                    path.write_text(json.dumps(document), encoding="utf-8")
                    router_pool = _parse_pools(document).get("p")
                    loaded = load_pools(path)
                loaded_pool = next((p for p in loaded if p.name == "p"), None)
                if router_pool is None or loaded_pool is None:
                    self.assertIsNone(router_pool)
                    self.assertIsNone(loaded_pool)
                    continue
                self.assertEqual([m.model for m in router_pool.members],
                                 [m.model for m in loaded_pool.members])
                self.assertEqual(router_pool.strategy, loaded_pool.strategy)


class NameCollisionTests(unittest.TestCase):
    def test_pool_named_after_model_is_rejected(self):
        import cx
        from modules.models import Model
        from modules.tui import PickerResult
        with patch.object(cx, "ensure_proxy", lambda: None), \
             patch.object(cx, "ensure_router", lambda: None), \
             patch.object(cx, "fetch_upstream_models",
                          lambda *a, **k: [Model("taken-model", "prov")]), \
             patch.object(cx, "fetch_models",
                          lambda *a, **k: [Model("taken-model", "prov")]), \
             patch.object(cx, "load_pools",
                          lambda *a, **k: [ModelPool("taken-model", (PoolMember("other-model"),))]), \
             patch.object(cx, "run_picker",
                          lambda *a, **k: PickerResult("exit", None, False, None)), \
             patch.object(cx, "pause_on_error", side_effect=SystemExit) as pause:
            with self.assertRaises(SystemExit):
                cx.main()
        self.assertIn("taken-model", str(pause.call_args))


class SweepSpacingTests(unittest.TestCase):
    def test_sweeps_are_spaced(self):
        from modules.router import _SWEEP_BACKOFF
        with patch("modules.router._POOL_PASSES", 2), _running_router(
            (500, {}, b"down"), (500, {}, b"down"),
            (500, {}, b"down"), (500, {}, b"down"),
        ) as router:
            began = time.monotonic()
            status, _ = _post(router)
            elapsed = time.monotonic() - began
        self.assertEqual(status, 503)
        self.assertGreaterEqual(elapsed, _SWEEP_BACKOFF)


class RotationTests(unittest.TestCase):
    def test_rotation_settles_once_per_request(self):
        members = [{"model": "provider/first", "priority": 0},
                   {"model": "provider/second", "priority": 0},
                   {"model": "provider/third", "priority": 0}]
        with patch("modules.router._POOL_PASSES", 1), _running_router(
            (500, {}, b"down"), (500, {}, b"down"), (500, {}, b"down"),
            members=members, strategy="round-robin",
        ) as router:
            status, _ = _post(router)
            self.assertEqual(status, 503)
            self.assertEqual(router.rotation.cursor("test-pool", 3), 1)

    def test_duplicate_member_does_not_stall_rotation(self):
        members = [{"model": "provider/first", "priority": 0},
                   {"model": "provider/first", "priority": 0},
                   {"model": "provider/second", "priority": 0}]
        with _running_router((200, {}, _OK_BODY), (200, {}, _OK_BODY), (200, {}, _OK_BODY),
                             members=members, strategy="round-robin") as router:
            for _ in range(3):
                self.assertEqual(_post(router)[0], 200)
        self.assertIn("provider/second", router.upstream_state.models)


class HeaderHygieneTests(unittest.TestCase):
    def test_no_duplicate_date_header(self):
        with _running_router((200, {}, _OK_BODY)) as router:
            connection = socket.create_connection(("127.0.0.1", router.server_port), timeout=10)
            body = json.dumps({"model": "test-pool", "messages": []}).encode()
            request = (f"POST /v1/messages HTTP/1.1\r\nHost: t\r\n"
                       f"Authorization: Bearer test-router-key\r\n"
                       f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
                       f"Connection: close\r\n\r\n").encode() + body
            connection.sendall(request)
            head = b""
            while b"\r\n\r\n" not in head:
                head += connection.recv(65536)
            connection.close()
        self.assertEqual(head.lower().count(b"\r\ndate:"), 1)
        self.assertEqual(head.lower().count(b"\r\nserver:"), 1)


class ResponseCapTests(unittest.TestCase):
    def test_oversized_response_is_rejected(self):
        with patch("modules.router._MAX_RESPONSE_BYTES", 4096), _running_router(
            (200, {}, b"x" * 9999), (200, {}, _OK_BODY),
        ) as router:
            status, body = _post(router)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), json.loads(_OK_BODY))
        self.assertFalse(router.cooldowns.is_ready("provider/first"))


class StopRouterTests(unittest.TestCase):
    def test_stop_router_without_pid_file(self):
        from modules import router_starter
        with patch.object(router_starter, "_read_pid", return_value=None), \
             patch.object(router_starter, "_listener_pids", return_value={4242}), \
             patch.object(router_starter, "_terminate_router", return_value=True) as kill, \
             patch.object(router_starter, "_port_is_open", return_value=False):
            self.assertTrue(router_starter.stop_router())
        kill.assert_called_once_with(4242)

    def test_stop_router_reports_failure_when_port_remains_open(self):
        from modules import router_starter
        with patch.object(router_starter, "_read_pid", return_value=123), \
             patch.object(router_starter, "_terminate_router", return_value=True), \
             patch.object(router_starter, "_port_is_open", return_value=True), \
             patch.object(router_starter, "_STOP_TIMEOUT", 0.3):
            self.assertFalse(router_starter.stop_router())


if __name__ == "__main__":
    unittest.main()
