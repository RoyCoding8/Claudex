"""Regression tests for router reliability."""
from __future__ import annotations

import gzip
import http.client
import io
import json
import logging
import socket
import socketserver
import struct
import sys
import tempfile
import threading
import time
import types
import unittest
from collections import deque
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext, redirect_stderr
from pathlib import Path
from typing import Any
from unittest.mock import patch

import modules.router as router_module
from modules import config, router_starter
from modules.pools import ModelPool, PoolMember, _atomic_publish, load_pools
from modules.router import (
    _SWEEP_BACKOFF,
    _UPSTREAM_TIMEOUT,
    _grammar_for,
    _ModelListCache,
    _parse_pools,
    _PoolRegistry,
    _RouterHandler,
    _RouterServer,
    _upstream_headers,
    _UpstreamResponse,
    _validate_body,
    run_forever,
)
from tests.test_router import (
    _OK_BODY,
    _SSE,
    _FakeUpstreamServer,
    _post,
    _running_router,
    _UpstreamHandler,
)


class StatusPhraseTests(unittest.TestCase):
    def test_an_unusual_status_code_reaches_the_client(self):
        for status, body in ((520, b"cloudflare hiccup"), (299, _OK_BODY)):
            with self.subTest(status=status), _running_router((status, {}, body)) as router:
                received, _ = _post(router, "/v1/completions", model="provider/direct")
            self.assertEqual(received, status)


class ServerLifecycleTests(unittest.TestCase):
    def test_server_can_rebind_immediately_after_close(self) -> None:
        router = _RouterServer(("127.0.0.1", 0))
        address = router.server_address
        self.assertTrue(router.socket.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR))
        router.server_close()
        rebound = _RouterServer(address)
        try:
            self.assertEqual(rebound.server_address, address)
        finally:
            rebound.server_close()

    def test_ipv6_binding_uses_ipv6_socket_family(self) -> None:
        router = _RouterServer(("::1", 0))
        try:
            self.assertEqual(router.address_family, socket.AF_INET6)
        finally:
            router.server_close()

    def test_binding_does_not_resolve_the_host_name(self) -> None:
        # HTTPServer.server_bind resolves the name between bind() and listen(), so
        # a resolver that stalls holds the port bound but unserved and the launcher's
        # health check fails for reasons that name no resolver. The router serves no
        # name, so the lookup has to be gone.
        resolved = []
        with patch("socket.getfqdn", side_effect=lambda host: resolved.append(host) or host):
            router = _RouterServer(("127.0.0.1", 0))
        try:
            self.assertEqual(resolved, [], "binding resolved the host name")
            self.assertEqual(router.server_name, "127.0.0.1")
        finally:
            router.server_close()


class EncodingTests(unittest.TestCase):
    def test_gzip_body_is_not_malformed(self):
        with _running_router(
            (200, {"Content-Encoding": "gzip"}, gzip.compress(_OK_BODY)),
            (200, {}, _OK_BODY),
        ) as router:
            status, body = _post(router)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), json.loads(_OK_BODY))
        self.assertEqual(router.upstream_state.models, ["provider/first"])

    def test_gzip_decode_removes_stale_strong_validators(self):
        with _running_router((
            200,
            {
                "Content-Encoding": "gzip",
                "Content-MD5": "old-md5",
                "Digest": "old-digest",
                "ETag": '"strong-etag"',
            },
            gzip.compress(_OK_BODY),
        )) as router:
            status, headers, received = _completions_post(
                router, b'{"model":"provider/direct","messages":[]}')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(received), json.loads(_OK_BODY))
        for name in ("content-md5", "digest", "etag"):
            self.assertNotIn(name, headers)

    def test_accept_encoding_not_forwarded(self):
        headers = _upstream_headers({"Accept-Encoding": "gzip, deflate, br", "X-Keep": "1"}, b"{}")
        self.assertEqual(headers.get("Accept-Encoding", "identity"), "identity")
        self.assertEqual(headers["X-Keep"], "1")

    def test_headers_are_deduplicated_case_insensitively(self) -> None:
        cases = (
            ("caller-supplied", {"X-Custom": "first", "x-custom": "second",
                                 "content-type": "text/plain"},
             {"x-custom": "first", "content-type": "text/plain"}),
            ("caller-default", {"Anthropic-Version": "2024-01-01", "Accept": "text/plain"},
             {"anthropic-version": "2024-01-01", "accept": "text/plain"}),
        )
        for label, incoming, expected in cases:
            with self.subTest(headers=label):
                headers = _upstream_headers(incoming, b"{}")
                for name, value in expected.items():
                    kept = [actual for actual in headers if actual.lower() == name]
                    self.assertEqual(len(kept), 1, f"{name} survived {len(kept)} times")
                    self.assertEqual(headers[kept[0]], value)


_START_FRAME = b'event: message_start\ndata: {"type":"message_start"}\n\n'
_DELTA_FRAME = b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"text":"hi"}}\n\n'
_STOP_FRAME = b'event: message_stop\ndata: {"type":"message_stop"}\n\n'


def _truncated(blob: bytes, dropped: int) -> bytes:
    return blob[:len(blob) - dropped]


_CORRUPT_DEFLATE = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x03" + bytes(range(40))


class GzipDecodeFailureTests(unittest.TestCase):
    def test_an_undecodable_gzip_response_fails_over_to_another_member(self) -> None:
        cases = (
            ("truncated_body", {}, _truncated(gzip.compress(_OK_BODY), 6)),
            ("corrupt_deflate", {}, _CORRUPT_DEFLATE),
            ("truncated_stream_head", _SSE, _truncated(gzip.compress(_START_FRAME), 6)),
        )
        for label, headers, blob in cases:
            with self.subTest(gzip=label), _running_router(
                (200, {**headers, "Content-Encoding": "gzip"}, blob),
                (200, {}, _OK_BODY),
            ) as router:
                status, body = _post(router)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), json.loads(_OK_BODY))
            self.assertEqual(router.upstream_state.models, ["provider/first", "provider/second"])

    def test_a_truncated_gzip_passthrough_body_is_reported_as_interrupted(self) -> None:
        for name, blob in (("truncated", _truncated(gzip.compress(_OK_BODY), 6)),
                           ("corrupt", _CORRUPT_DEFLATE)):
            with self.subTest(gzip=name):
                with _running_router((200, {"Content-Encoding": "gzip"}, blob)) as router:
                    status, body = _raw_post(router, _direct_post())
                self.assertEqual(status, 502)
                self.assertIn(b"router: upstream response interrupted", body)

    def test_a_truncated_gzip_stream_ends_with_an_error_frame(self) -> None:
        with _running_router(
            (200, {**_SSE, "Content-Encoding": "gzip"},
             (_truncated(gzip.compress(_START_FRAME + _DELTA_FRAME), 6), b"\r\n")),
            (200, {}, _OK_BODY),
        ) as router:
            status, body = _post(router)
        self.assertEqual(status, 200)
        self.assertIn(b'"type":"error"', body)
        self.assertIn(b"upstream stream interrupted", body)


def _direct_attempts(count: int) -> Any:
    return patch.multiple("modules.router", _DIRECT_ATTEMPTS=count, _DIRECT_BACKOFF=0.0)


def _pool_passes(count: int) -> Any:
    return patch.multiple("modules.router", _POOL_PASSES=count, _SWEEP_BACKOFF=0.0)


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


class RequestHeaderResetTests(unittest.TestCase):
    def test_client_reset_during_request_headers_is_contained(self) -> None:
        handler = _RouterHandler.__new__(_RouterHandler)
        handler.close_connection = False
        with patch("modules.router.BaseHTTPRequestHandler.handle_one_request",
                   side_effect=ConnectionResetError(104, "reset")):
            handler.handle_one_request()
        self.assertTrue(handler.close_connection)


class FakeServerOutputTests(unittest.TestCase):
    def test_fake_upstream_reset_does_not_write_traceback(self) -> None:
        output = io.StringIO()
        with _pool_passes(1), redirect_stderr(output), _running_router(
            (200, {**_SSE, "Content-Length": "999"}, (_START_FRAME, b"__drop__")),
        ) as router:
            connection = socket.create_connection(("127.0.0.1", router.server_port), timeout=10)
            body = json.dumps({"model": "test-pool", "messages": []}).encode()
            connection.sendall(
                (f"POST /v1/messages HTTP/1.1\r\nHost: t\r\n"
                 f"Authorization: Bearer test-router-key\r\n"
                 f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
                 f"Connection: close\r\n\r\n").encode() + body
            )
            head = b""
            while b"\r\n\r\n" not in head:
                head += connection.recv(65536)
            router.upstream_state.stream_gate.set()
            connection.close()
        self.assertNotIn("Exception occurred during processing of request", output.getvalue())


class _StubConn:
    def __init__(self): self.closed = 0
    def close(self): self.closed += 1


def _completions_post(router: _RouterServer, body: bytes = b"{}",
                      path: str = "/v1/completions") -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", router.server_port, timeout=10)
    connection.request(
        "POST", path, body=body,
        headers={"Authorization": "Bearer test-router-key", "Content-Length": str(len(body))},
    )
    response = connection.getresponse()
    result = (response.status,
              {key.lower(): value for key, value in response.getheaders()},
              response.read())
    connection.close()
    return result


def _sse_upstream(chunks, conn=None):
    return _UpstreamResponse(200, "OK", [("Content-Type", "text/event-stream")],
                             iter(chunks), conn or _StubConn())


class _IPv6UpstreamServer(_FakeUpstreamServer):
    address_family = socket.AF_INET6


class _IPv6UpstreamHandler(_UpstreamHandler):
    def do_POST(self) -> None:
        self.server.state.hosts.append(self.headers["Host"])
        super().do_POST()


class ServerIdentityTests(unittest.TestCase):
    def test_the_server_header_names_the_router_and_not_the_interpreter(self) -> None:
        with _running_router((200, {}, _OK_BODY)) as router:
            connection = http.client.HTTPConnection("127.0.0.1", router.server_port, timeout=5)
            connection.request("GET", "/health")
            response = connection.getresponse()
            server = response.getheader("Server")
            body = response.read()
            connection.close()
        self.assertEqual(server, f"{config.ROUTER_IDENTITY} ")
        self.assertEqual(json.loads(body), {"status": "ok"})

    def test_the_starters_readiness_probe_accepts_the_reduced_server_header(self) -> None:
        with _running_router((200, {}, _OK_BODY)) as router, \
             patch.object(router_starter, "ROUTER_HOST", "127.0.0.1"), \
             patch.object(router_starter, "ROUTER_PORT", router.server_port):
            self.assertTrue(router_starter._health_check())


class IPv6AuthorityTests(unittest.TestCase):
    def test_attested_endpoint_uses_bracketed_ipv6_router_authority(self) -> None:
        response = b'{"model":"provider/first","content":[{"type":"text","text":"ok"}]}'
        with patch("modules.router.ROUTER_HOST", "::1"), patch("modules.router.ROUTER_PORT", 4000), \
                _running_router((200, {}, response)) as router:
            status, body = _post(router)

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["endpoint"], "http://[::1]:4000/v1")

    def test_upstream_host_uses_bracketed_ipv6_without_bracketing_socket_target(self) -> None:
        upstream = _IPv6UpstreamServer(("::1", 0), _IPv6UpstreamHandler)
        upstream.state = types.SimpleNamespace(
            responses=deque([(200, {}, _OK_BODY)]), models=[], paths=[], hosts=[],
            stream_gate=threading.Event(),
        )
        upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        upstream_thread.start()
        try:
            with patch("modules.router.PROXY_HOST", "::1"), patch(
                "modules.router.PROXY_PORT", upstream.server_port
            ), patch("modules.router.ROUTER_API_KEY", "test-router-key"):
                router = _RouterServer(("127.0.0.1", 0))
                router_thread = threading.Thread(target=router.serve_forever, daemon=True)
                router_thread.start()
                try:
                    status, body = _post(router, "/v1/completions", model="provider/direct")
                finally:
                    router.shutdown()
                    router.server_close()
                    router_thread.join(timeout=2)
        finally:
            upstream.shutdown()
            upstream.server_close()
            upstream_thread.join(timeout=2)

        self.assertEqual((status, body), (200, _OK_BODY))
        self.assertEqual(upstream.state.hosts, [f"[::1]:{upstream.server_port}"])

    def test_the_unreachable_upstream_message_brackets_an_ipv6_host(self) -> None:
        for host, expected in (("::1", "http://[::1]:1"), ("127.0.0.1", "http://127.0.0.1:1"),
                               ("localhost", "http://localhost:1")):
            with self.subTest(host=host), patch("modules.router.PROXY_HOST", host), \
                 patch("modules.router.PROXY_PORT", 1), \
                 patch("modules.router.socket.create_connection", side_effect=OSError("refused")), \
                 patch("modules.router.time.sleep"), \
                 patch("modules.router._now", side_effect=[0.0, 10.0]):
                with self.assertRaises(RuntimeError) as unreachable:
                    router_module._wait_upstream(deadline=1.0)
            self.assertEqual(str(unreachable.exception),
                             f"CLIProxyAPI unreachable at {expected} — start it before the router.")

    def test_the_startup_log_brackets_an_ipv6_host(self) -> None:
        class Server:
            def serve_forever(self, **kwargs: object) -> None:
                pass

            def server_close(self) -> None:
                pass

        with patch("modules.router._configure_logging"), \
             patch("modules.router.ROUTER_HOST", "::1"), patch("modules.router.ROUTER_PORT", 4000), \
             patch("modules.router.PROXY_HOST", "::2"), patch("modules.router.PROXY_PORT", 8317), \
             patch("modules.router._wait_upstream"), \
             patch("modules.router._RouterServer", return_value=Server()), \
             self.assertLogs(router_module._LOG, level="INFO") as captured:
            self.assertEqual(run_forever(), 0)
        self.assertIn("router starting on http://[::1]:4000, forwarding to http://[::2]:8317",
                      "\n".join(captured.output))


class ResponseAttestationTests(unittest.TestCase):
    def test_mutated_body_drops_stale_entity_validators(self) -> None:
        body = b'{"model":"provider/first","content":[{"type":"text","text":"ok"}]}'
        with _running_router((200, {
            "Content-MD5": "old-md5",
            "Digest": "old-digest",
            "ETag": 'W/"old-etag"',
        }, body)) as router:
            status, headers, received = _completions_post(
                router, b'{"model":"test-pool","messages":[]}', "/v1/messages")
        self.assertEqual(status, 200)
        self.assertNotEqual(received, body)
        for name in ("content-md5", "digest", "etag"):
            self.assertNotIn(name, headers)

    def test_unchanged_weak_etag_survives_passthrough(self) -> None:
        with _running_router((400, {"ETag": 'W/"stable"'}, b'{"error":"bad"}')) as router:
            status, headers, received = _completions_post(
                router, b'{"model":"provider/direct","messages":[]}')
        self.assertEqual((status, received), (400, b'{"error":"bad"}'))
        self.assertEqual(headers["etag"], 'W/"stable"')

    def test_terminal_error_bodies_are_not_attested(self) -> None:
        body = b'{"model":"provider/first","content":[{"type":"text","text":"bad"}]}'
        for path, model, status_code in (
            ("/v1/messages", "test-pool", 400),
            ("/v1/completions", "provider/direct", 500),
        ):
            with self.subTest(path=path), _running_router((status_code, {}, body)) as router:
                received_status, received_body = _post(router, path, model=model)
            if model == "test-pool":
                # A refusal is a member failure, so the pool is swept and the
                # client is told the pool failed rather than handed one member's
                # body as though it were the answer.
                self.assertEqual(received_status, 503)
                self.assertNotIn(b"content", received_body)
            else:
                self.assertEqual((received_status, received_body), (status_code, body))

    def test_direct_non_pooled_success_is_not_attested(self) -> None:
        body = b'{"model":"provider/direct","content":[{"type":"text","text":"ok"}]}'
        with _running_router((200, {}, body)) as router:
            status, received = _post(router, "/v1/completions", model="provider/direct")
        self.assertEqual(status, 200)
        self.assertEqual(received, body)

    def test_upstream_header_injection_is_rejected_before_forwarding(self) -> None:
        upstream = _UpstreamResponse(
            200,
            "OK",
            [("X-Bad", "ok\r\nInjected: yes")],
            iter((_OK_BODY,)),
            _StubConn(),
        )
        with patch("modules.router._forward_to_upstream", return_value=upstream), _running_router() as router:
            connection = http.client.HTTPConnection("127.0.0.1", router.server_port, timeout=10)
            request_body = b"{}"
            connection.request(
                "POST", "/v1/completions", body=request_body,
                headers={"Authorization": "Bearer test-router-key", "Content-Length": str(len(request_body))},
            )
            response = connection.getresponse()
            received = response.read()
            response_headers = {key.lower() for key, _ in response.getheaders()}
            connection.close()
        self.assertEqual(response.status, 502)
        self.assertIn(b"invalid upstream headers", received)
        self.assertNotIn("injected", response_headers)

    def test_all_forbidden_c0_header_values_are_rejected(self) -> None:
        with patch("modules.router._forward_to_upstream") as forward, _running_router() as router:
            for code in range(0x20):
                if code == 0x09:
                    continue
                with self.subTest(code=code):
                    forward.return_value = _UpstreamResponse(
                        200, "OK", [("X-Bad", f"ok{chr(code)}value")], iter((_OK_BODY,)), _StubConn())
                    status, _, received = _completions_post(router)
                    self.assertEqual(status, 502)
                    self.assertIn(b"invalid upstream headers", received)

    def test_header_names_must_use_legal_tokens(self) -> None:
        invalid_names = [f"X-Bad{chr(code)}" for code in range(0x20)]
        invalid_names.extend(("X Bad", "X/Bad", "X@Bad", "Xé"))
        with patch("modules.router._forward_to_upstream") as forward, _running_router() as router:
            for name in invalid_names:
                with self.subTest(name=name):
                    forward.return_value = _UpstreamResponse(
                        200, "OK", [(name, "value")], iter((_OK_BODY,)), _StubConn())
                    status, _, received = _completions_post(router)
                    self.assertEqual(status, 502)
                    self.assertIn(b"invalid upstream headers", received)

    def test_legal_header_name_and_horizontal_tab_value_are_preserved(self) -> None:
        upstream = _UpstreamResponse(
            200,
            "OK",
            [("X-Legal_1", "value\twith-tab")],
            iter((_OK_BODY,)),
            _StubConn(),
        )
        with patch("modules.router._forward_to_upstream", return_value=upstream), _running_router() as router:
            status, headers, received = _completions_post(router)
        self.assertEqual(status, 200)
        self.assertEqual(received, _OK_BODY)
        self.assertEqual(headers["x-legal_1"], "value\twith-tab")


class RequestBodyBoundaryTests(unittest.TestCase):
    SCRIPT = (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
              + str(len(_OK_BODY)).encode() + b"\r\n\r\n" + _OK_BODY)

    def test_a_lone_surrogate_in_the_body_reaches_the_upstream_unchanged(self) -> None:
        body = b'{"model":"test-pool","note":"\\ud800","messages":[]}'
        output = io.StringIO()
        with redirect_stderr(output), _running_raw_upstream(self.SCRIPT) as (router, state):
            responses = _raw_responses(router, _direct_post(b"/v1/messages", body))
        self.assertEqual(responses, [(200, _OK_BODY)])
        forwarded = json.loads(state.request_bodies[0])
        self.assertEqual(forwarded["note"], "\ud800")
        self.assertEqual(forwarded["model"], "provider/first")
        self.assertNotIn("UnicodeEncodeError", output.getvalue())
        self.assertNotIn("Exception occurred during processing of request", output.getvalue())

    def test_a_lone_surrogate_survives_every_json_string_position(self) -> None:
        document = ('{"model":"test-pool","a":"\\ud800","b":["\\udfff"],'
                    '"c":{"d":"\\ud83d"},"messages":[]}')
        with _running_raw_upstream(self.SCRIPT) as (router, state):
            responses = _raw_responses(router, _direct_post(b"/v1/messages", document.encode("ascii")))
        self.assertEqual(responses, [(200, _OK_BODY)])
        forwarded = json.loads(state.request_bodies[0])
        self.assertEqual(
            [forwarded["a"], forwarded["b"], forwarded["c"]],
            ["\ud800", ["\udfff"], {"d": "\ud83d"}],
        )

    def test_a_high_surrogate_pair_and_non_ascii_text_reach_the_upstream_unchanged(self) -> None:
        cases = (("surrogate_pair", b'{"model":"test-pool","note":"\\ud83d\\ude00","messages":[]}',
                  "\U0001f600"),
                 ("non_ascii", '{"model":"test-pool","note":"héllo — 世界","messages":[]}'.encode(),
                  "héllo — 世界"))
        for label, body, note in cases:
            with self.subTest(text=label), _running_raw_upstream(self.SCRIPT) as (router, state):
                responses = _raw_responses(router, _direct_post(b"/v1/messages", body))
            self.assertEqual(responses, [(200, _OK_BODY)])
            self.assertEqual(json.loads(state.request_bodies[0])["note"], note)


class ResponseBodyAttestationBoundaryTests(unittest.TestCase):
    def _script(self, body: bytes) -> bytes:
        return (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                + str(len(body)).encode() + b"\r\n\r\n" + body)

    def _post_pooled(self, router) -> list[tuple[int, bytes]]:
        return _raw_responses(router, _direct_post(
            b"/v1/messages", b'{"model":"test-pool","messages":[]}'))

    def test_unencodable_upstream_text_is_still_attested(self) -> None:
        for label, text in (("lone_surrogate", "\ud800"), ("non_ascii", "héllo — 世界")):
            with self.subTest(text=label):
                body = json.dumps({"model": "provider/first", "type": "message",
                                   "content": [{"type": "text", "text": text}]}).encode(
                    "ascii" if label == "lone_surrogate" else "utf-8")
                with _running_raw_upstream(self._script(body)) as (router, _):
                    responses = self._post_pooled(router)
                self.assertEqual(len(responses), 1)
                attested = json.loads(responses[0][1])
                self.assertEqual(attested["content"], [{"type": "text", "text": text}])
                self.assertEqual(attested["provider"], "provider")
                self.assertEqual(attested["endpoint"],
                                 f"http://127.0.0.1:{router_module.ROUTER_PORT}/v1")


class StreamHeadDeadlineTests(unittest.TestCase):
    HEAD = b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n"

    def test_a_head_peek_that_never_concludes_cools_the_member(self) -> None:
        drip = [(0.0, self.HEAD)] + [(0.4, b"x") for _ in range(12)]
        members = [{"model": "provider/first", "priority": 1}]
        with (patch("modules.router._POOL_REQUEST_TIMEOUT", 3.0),
              patch("modules.router._POOL_PASSES", 1),
              _running_raw_upstream(drip=drip, members=members) as (router, state)):
            responses = _raw_responses(router, _direct_post(
                b"/v1/messages", b'{"model":"test-pool","messages":[]}'))
        self.assertEqual([status for status, _ in responses], [503])
        self.assertFalse(b"text/event-stream" in responses[0][1])
        self.assertFalse(router.cooldowns.is_ready("provider/first"))
        self.assertEqual(len(state.request_lines), 1)

    def test_a_stream_head_that_lands_content_still_validates(self) -> None:
        drip = [(0.0, self.HEAD),
                (0.2, b"event: content_block_delta\n"
                     b'data: {"type":"content_block_delta","index":0,'
                     b'"delta":{"type":"text_delta","text":"hi"}}\n\n'),
                (0.2, b"event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n")]
        members = [{"model": "provider/first", "priority": 1}]
        with (patch("modules.router._POOL_REQUEST_TIMEOUT", 3.0),
              patch("modules.router._POOL_PASSES", 1),
              _running_raw_upstream(drip=drip, members=members) as (router, _)):
            wire = _raw_exchange(router, _direct_post(
                b"/v1/messages", b'{"model":"test-pool","messages":[]}'))
        head, _, body = wire.partition(b"\r\n\r\n")
        self.assertEqual(head.split(b"\r\n", 1)[0], b"HTTP/1.1 200 OK")
        self.assertIn(b"event: content_block_delta", body)
        self.assertIn(b"event: message_stop", body)
        self.assertNotIn(b"upstream stream interrupted", body)
        self.assertTrue(router.cooldowns.is_ready("provider/first"))


class UpstreamStatusLineTests(unittest.TestCase):
    def test_no_byte_outside_the_field_value_grammar_survives_in_the_status_line(self) -> None:
        cases = (
            ("nul", b"oo\0ps", b"HTTP/1.1 520 "),
            ("del", b"oo\x7fps", b"HTTP/1.1 520 "),
            ("cr", b"oo\rps", b"HTTP/1.1 520 "),
            ("lf", b"oo\nps", b"HTTP/1.1 520 oo"),
            ("escape", b"oo\x1bps", b"HTTP/1.1 520 "),
            ("obs_text", b"oo\x85ps", b"HTTP/1.1 520 oo\x85ps"),
        )
        for label, reason, expected in cases:
            with self.subTest(reason=label):
                script = (b"HTTP/1.1 520 " + reason + b"\r\n"
                          b"Content-Type: application/json\r\nContent-Length: 2\r\n\r\nhi")
                with _running_raw_upstream(script) as (router, _):
                    wire = _raw_exchange(router, _direct_post())
                self.assertEqual(wire.split(b"\r\n\r\n", 1)[0].split(b"\r\n", 1)[0], expected)

    def test_a_legal_reason_phrase_and_a_known_code_reach_the_client(self) -> None:
        cases = (
            (b"HTTP/1.1 520 Cloudflare oops\r\nContent-Length: 2\r\n\r\nhi",
             b"HTTP/1.1 520 Cloudflare oops", ()),
            (b"HTTP/1.1 520 5.1\tkept tab\r\nContent-Length: 2\r\n\r\nhi",
             b"HTTP/1.1 520 5.1\tkept tab", ()),
            (b"HTTP/1.1 520 oops\rX-Injected: yes\r\n"
             b"Content-Type: application/json\r\nContent-Length: 2\r\n\r\nhi",
             b"HTTP/1.1 520 ", (b"X-Injected", b"oops")),
            (b"HTTP/1.1 404 oops\rX-Injected: yes\r\nContent-Length: 2\r\n\r\nhi",
             b"HTTP/1.1 404 Not Found", (b"X-Injected",)),
            (b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nhi", b"HTTP/1.1 200 OK", ()),
        )
        for script, expected, forbidden in cases:
            with self.subTest(status=script.split(b"\r\n", 1)[0]):
                with _running_raw_upstream(script) as (router, _):
                    wire = _raw_exchange(router, _direct_post())
                self.assertEqual(wire.split(b"\r\n\r\n", 1)[0].split(b"\r\n", 1)[0], expected)
                self.assertIn(b"hi", wire, "the upstream body did not survive the status line")
                for token in forbidden:
                    self.assertNotIn(token, wire)

    def test_a_header_value_is_judged_by_the_field_value_grammar(self) -> None:
        cases = (("delete_byte", b"a\x7fb", b"HTTP/1.1 502 Bad Gateway",
                  b"invalid upstream headers", b"X-V: a"),
                 ("horizontal_tab", b"a\tb", b"HTTP/1.1 200 OK", b"X-V: a\tb", b"invalid"))
        for label, value, expected_status, expected_in_wire, forbidden in cases:
            with self.subTest(value=label):
                script = (b"HTTP/1.1 200 OK\r\nX-V: " + value + b"\r\n"
                          b"Content-Type: application/json\r\nContent-Length: 2\r\n\r\nhi")
                with _running_raw_upstream(script) as (router, _):
                    wire = _raw_exchange(router, _direct_post())
                self.assertEqual(wire.split(b"\r\n\r\n", 1)[0].split(b"\r\n", 1)[0], expected_status)
                self.assertIn(expected_in_wire, wire)
                self.assertNotIn(forbidden, wire)


def _interrupted_stream() -> Iterator[bytes]:
    yield _START_FRAME
    raise ConnectionResetError(104, "upstream reset")


class ClientAbortTests(unittest.TestCase):
    def test_a_dead_client_is_a_disconnect_and_does_not_cool_the_member(self):
        cases = (("broken_pipe_midstream", 1, BrokenPipeError(32, "The pipe has been ended")),
                 ("write_timeout_on_first_frame", 0, TimeoutError("client stopped draining")))
        for label, fail_after, error in cases:
            with self.subTest(failure=label):
                handler = _bare_handler(_DeadClient(fail_after=fail_after, error=error))
                upstream = _sse_upstream([_START_FRAME, _DELTA_FRAME, _STOP_FRAME])
                self.assertTrue(handler._stream_upstream(upstream, request_id="t", member="m"))

    def test_an_upstream_drop_still_cools_the_member(self):
        cases = (("client_still_reading", _DeadClient(fail_after=99)),
                 ("client_write_timeout",
                  _DeadClient(1, error=TimeoutError("client stopped draining"))))
        for label, client in cases:
            with self.subTest(failure=label):
                handler = _bare_handler(client)
                self.assertFalse(handler._stream_upstream(
                    _sse_upstream(_interrupted_stream()), request_id="t", member="m"))

    def test_client_write_timeout_on_buffered_body_is_swallowed(self):
        handler = _bare_handler(_DeadClient(0, error=TimeoutError("client stopped draining")))
        upstream = _UpstreamResponse(200, "OK", [("Content-Type", "application/json")],
                                     iter(()), _StubConn(), buffered=_OK_BODY)
        self.assertTrue(handler._stream_upstream(upstream, request_id="t", member="m"))

    def test_send_json_swallows_client_disconnects(self) -> None:
        for error in (
            TimeoutError("client stopped draining"),
            ConnectionResetError(104, "reset"),
            ConnectionAbortedError(103, "aborted"),
        ):
            with self.subTest(error=type(error).__name__):
                handler = _bare_handler(_DeadClient(0, error=error))
                self.assertIsNone(handler._send_json(200, {"ok": True}))


@contextmanager
def _root_logging() -> Iterator[logging.Logger]:
    root = logging.getLogger()
    handlers, level = root.handlers, root.level
    try:
        yield root
    finally:
        root.handlers, root.level = handlers, level


class LogSafetyTests(unittest.TestCase):
    def test_the_fallback_sink_stops_growing_once_its_budget_is_spent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            blocked = base / "not-a-directory"
            blocked.write_bytes(b"x")
            boot = base / "router.boot.log"
            with boot.open("at") as stream:
                with (
                    patch.object(router_module, "ROUTER_LOG", blocked / "router.log"),
                    patch.object(sys, "stderr", stream),
                    _root_logging(),
                ):
                    router_module._configure_logging()
                    sizes = []
                    for _ in range(120):
                        router_module._LOG.info("x" * 100_000)
                        sizes.append(boot.stat().st_size)
            self.assertGreater(sizes[0], 0, "the emergency sink recorded nothing, so the bound is untested")
            self.assertEqual(
                sizes[-1], sizes[-2],
                "the emergency sink kept growing after its budget was spent, so a long-lived "
                "router appends to data/router.boot.log forever")
            self.assertFalse((blocked / "router.log").exists())

    def test_rate_limit_headers_are_not_logged(self) -> None:
        with self.assertLogs("cx.router", level="INFO") as logs:
            with _running_router(
                (429, {"Retry-After": "7", "X-RateLimit-Token": "opaque-rate-limit-value"},
                 b'{"error":"provider secret in body"}'),
                (200, {}, _OK_BODY),
            ) as router:
                status, _ = _post(router)
        output = "\n".join(logs.output)
        self.assertEqual(status, 200)
        self.assertNotIn("opaque-rate-limit-value", output)
        self.assertNotIn("provider secret in body", output)


class PostCommitTests(unittest.TestCase):
    def test_client_disconnect_after_commit_does_not_retry_or_cool_member(self):
        first_frame = _START_FRAME + _DELTA_FRAME
        big_delta = (b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"text":"'
                     + b"x" * 16_000_000 + b'"}}\n\n')
        with patch.object(_RouterHandler, "timeout", 0.3), patch(
            "modules.router._MAX_SSE_FRAME_BYTES", 32 * 1024 * 1024
        ), _running_router(
            (200, _SSE, (first_frame, big_delta)),
        ) as router:
            connection = socket.create_connection(("127.0.0.1", router.server_port), timeout=10)
            body = json.dumps({"model": "test-pool", "messages": []}).encode()
            connection.sendall(
                (f"POST /v1/messages HTTP/1.1\r\nHost: t\r\n"
                 f"Authorization: Bearer test-router-key\r\n"
                 f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
                 f"Connection: close\r\n\r\n").encode() + body)
            received = b""
            while b"\r\n\r\n" not in received or len(received.split(b"\r\n\r\n", 1)[1]) < len(first_frame):
                received += connection.recv(65536)
            head, initial = received.split(b"\r\n\r\n", 1)
            self.assertIn(b"HTTP/1.1 200", head)
            self.assertEqual(initial[:len(first_frame)], first_frame)
            router.upstream_state.stream_gate.set()
            connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            connection.close()
            deadline = time.monotonic() + 2.0
            while router.inflight.count("provider/first") and time.monotonic() < deadline:
                time.sleep(0.05)
        self.assertEqual(router.upstream_state.models, ["provider/first"])
        self.assertTrue(router.cooldowns.is_ready("provider/first"))


class AuthHardeningTests(unittest.TestCase):
    def test_a_rejected_body_is_drained_so_the_401_is_not_reset_away(self) -> None:
        """A rejected request must not reset the connection before the 401 lands.

        Clients write the headers and the body as two sends, so on the rejection
        path the body is still unread when the response goes out. Closing a socket
        that still holds unread data makes the peer reset it instead of reading
        the response, so the client sees a connection error rather than its 401.
        """
        body = b'{"model":"test-pool","messages":[]}'
        with _running_router(*[(200, {}, _OK_BODY) for _ in range(3)]) as router:
            for _ in range(50):
                connection = http.client.HTTPConnection("127.0.0.1", router.server_port, timeout=10)
                # Two sends, not one: this is what http.client does, and it is
                # what leaves the body unread at the moment the 401 is written.
                connection.putrequest("POST", "/v1/messages", skip_accept_encoding=True)
                connection.putheader("Authorization", "Bearer wrong")
                connection.putheader("Content-Type", "application/json")
                connection.putheader("Content-Length", str(len(body)))
                connection.endheaders()
                connection.send(body)
                response = connection.getresponse()
                self.assertEqual(response.status, 401)
                response.read()
                connection.close()

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

    def test_bearer_scheme_casing_and_separator_whitespace(self) -> None:
        request_body = json.dumps({"model": "test-pool", "messages": []}).encode()
        valid = ("bEaReR test-router-key", "Bearer\t test-router-key", "Bearer   test-router-key")
        invalid = ("Bearerkey", "Bearer test-router-key extra", "Bearer")
        with _running_router(*[(200, {}, _OK_BODY) for _ in valid]) as router:
            outcomes = []
            for value in valid + invalid:
                connection = http.client.HTTPConnection("127.0.0.1", router.server_port, timeout=10)
                connection.request(
                    "POST",
                    "/v1/messages",
                    body=request_body,
                    headers={
                        "Authorization": value,
                        "Content-Type": "application/json",
                        "Content-Length": str(len(request_body)),
                    },
                )
                response = connection.getresponse()
                outcomes.append(response.status)
                response.read()
                connection.close()
        self.assertEqual(outcomes, [200, 200, 200, 401, 401, 401])

    def test_a_rejected_key_produces_exactly_one_response(self) -> None:
        body = b'{"model":"test-pool","messages":[]}'
        requests = {
            "models_bearer": b"GET /v1/models HTTP/1.1\r\nHost: t\r\n"
                            b"Authorization: Bearer wrong\r\n\r\n",
            "models_no_header": b"GET /v1/models HTTP/1.1\r\nHost: t\r\n\r\n",
            "completions_bearer": b"POST /v1/completions HTTP/1.1\r\nHost: t\r\n"
                                  b"Authorization: Bearer wrong\r\n"
                                  b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body,
            "completions_no_header": b"POST /v1/completions HTTP/1.1\r\nHost: t\r\n"
                                    b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body,
            "messages_bearer": b"POST /v1/messages HTTP/1.1\r\nHost: t\r\n"
                               b"Authorization: Bearer wrong\r\n"
                               b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body,
            "count_tokens_bearer": b"POST /v1/messages/count_tokens HTTP/1.1\r\nHost: t\r\n"
                                   b"Authorization: Bearer wrong\r\n"
                                   b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body,
        }
        for label, request in requests.items():
            with self.subTest(request=label), _running_router((200, {}, _OK_BODY)) as router:
                responses = _raw_responses(router, request)
                self.assertEqual(responses, [(401, b'{"error": {"message": "invalid api key"}}')])
                self.assertEqual(router.upstream_state.models, [])
                self.assertEqual(router.upstream_state.paths, [])

    def test_a_rejected_key_does_not_cool_a_member(self) -> None:
        body = b'{"model":"test-pool","messages":[]}'
        request = (b"POST /v1/messages HTTP/1.1\r\nHost: t\r\n"
                   b"Authorization: Bearer wrong\r\n"
                   b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
        with _running_router((200, {}, _OK_BODY), (200, {}, _OK_BODY)) as router:
            self.assertEqual(_raw_responses(router, request), [(401, b'{"error": {"message": "invalid api key"}}')])
        self.assertTrue(router.cooldowns.is_ready("provider/first"))
        self.assertTrue(router.cooldowns.is_ready("provider/second"))
        self.assertEqual(router.upstream_state.models, [])


class ConnectionLifetimeTests(unittest.TestCase):
    def test_failed_attempt_closes_before_next_dispatch(self):
        original = http.client.HTTPConnection
        connections, events = [], []

        class Counted(original):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                connections.append(self)
                events.append("connect")

            def close(self):
                events.append("close")
                super().close()

        with patch("modules.router.HTTPConnection", Counted), _running_router(
            (200, {}, b"not json"),
            (200, {}, _OK_BODY),
        ) as router:
            status, _ = _post(router)
        self.assertEqual(status, 200)
        self.assertEqual(events, ["connect", "close", "connect", "close"])
        self.assertEqual(len(connections), 2)


class PoolRegistryTests(unittest.TestCase):
    def test_a_pool_document_the_router_cannot_read_is_survivable(self) -> None:
        readable = b'{"pools":[{"name":"p","members":[{"model":"a"}]}]}'
        documents = (
            ("non_utf8", b'{"pools":[{"name":"p\xff","members":[{"model":"a"}]}]}', {}),
            ("json_recursion", b'{"pools":[{"name":"p","members":[{"model":"a"}],"extra":'
                               + b"[" * 10000 + b"0" + b"]" * 10000 + b"}]}", {}),
            ("json_integer_limit", b'{"pools":[{"name":"p","members":[{"model":"a"}],"extra":'
                                  + b"9" * 5000 + b"}]}", {}),
            # Trailing spaces keep this valid json, so only the length check refuses it.
            ("over_the_read_cap", readable + b" " * 20, {"_MAX_POOL_FILE_BYTES": 50}),
        )
        for label, document, caps in documents:
            with self.subTest(document=label), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "pools.json"
                path.write_bytes(document)
                capped = patch.multiple("modules.router", create=True, **caps) if caps else nullcontext()
                with capped:
                    self.assertIsNone(_PoolRegistry(path).get("p"))

    def test_a_stat_that_under_reports_the_size_does_not_defeat_the_cap(self) -> None:
        document = b'{"pools":[{"name":"p","members":[{"model":"a"}]}]}' + b" " * 20
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pools.json"
            path.write_bytes(document)
            real_stat = Path.stat

            def under_reporting(self, *args, **kwargs):
                if self == path:
                    return types.SimpleNamespace(
                        st_mtime_ns=0, st_size=0, st_ino=0, st_ctime_ns=0)
                return real_stat(self, *args, **kwargs)

            with patch.object(Path, "stat", under_reporting), \
                 patch("modules.router._MAX_POOL_FILE_BYTES", 50):
                self.assertIsNone(_PoolRegistry(path).get("p"))

    def test_an_oversized_pool_document_is_never_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pools.json"
            path.write_bytes(b'{"pools":[{"name":"p","members":[{"model":"a"}]}]}')
            opens: list[int] = []
            real_open = Path.open

            def counting_open(self, *args, **kwargs):
                if self == path:
                    opens.append(1)
                return real_open(self, *args, **kwargs)

            with patch.object(Path, "open", counting_open), \
                 patch("modules.router._POOLS_STAT_INTERVAL", 0.0), \
                 patch("modules.router._MAX_POOL_FILE_BYTES", 20):
                registry = _PoolRegistry(path)
                for _ in range(5):
                    self.assertIsNone(registry.get("p"))
        self.assertEqual(opens, [])

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

    def test_transient_pool_read_failure_retries_same_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pools.json"
            path.write_text(json.dumps({"pools": [{"name": "p", "members": [{"model": "a"}]}]}))
            registry = _PoolRegistry(path)
            real_open = Path.open
            attempts = 0

            def flaky_open(self, *args, **kwargs):
                nonlocal attempts
                if self == path and attempts == 0:
                    attempts += 1
                    raise OSError("transient read failure")
                return real_open(self, *args, **kwargs)

            with patch.object(Path, "open", flaky_open), \
                 patch("modules.router._POOLS_STAT_INTERVAL", 0.0):
                self.assertIsNone(registry.get("p"))
                self.assertIsNotNone(registry.get("p"))

    def test_a_same_length_rewrite_is_still_detected(self) -> None:
        # save_pools renames a temp file over the target, so the new inode's mtime
        # is fresh; (mtime, size, inode, ctime) then tells a same-length rewrite.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pools.json"
            first = {"pools": [{"name": "p", "members": [{"model": "aa"}]}]}
            second = {"pools": [{"name": "p", "members": [{"model": "bb"}]}]}
            _atomic_publish(path, json.dumps(first))
            registry = _PoolRegistry(path)
            with patch("modules.router._POOLS_STAT_INTERVAL", 0.0):
                self.assertEqual(registry.get("p").members[0].model, "aa")
                before = path.stat()
                _atomic_publish(path, json.dumps(second))
                after = path.stat()
                self.assertEqual(after.st_size, before.st_size, "the two bodies must differ only in content")
                self.assertNotEqual(
                    (after.st_mtime_ns, after.st_ino),
                    (before.st_mtime_ns, before.st_ino),
                    "a publish that preserved the identity would let the registry serve a stale document",
                )
                self.assertEqual(registry.get("p").members[0].model, "bb")


class PoolReloadBoundaryTests(unittest.TestCase):
    GOOD = '{"version":1,"pools":[{"name":"live","members":[{"model":"p/first"}]}]}'
    REFUSALS = {
        "duplicate_member_key": '{"name":"d","members":[{"model":"p/first","model":"p/second"}]}',
        "duplicate_version": '{"version":1,"version":2,"pools":[{"name":"live","members":[{"model":"p/first"}]}]}',
        "duplicate_pools_key": '{"version":1,"pools":[],"pools":[{"name":"live","members":[{"model":"p/first"}]}]}',
        "duplicate_top_level": '{"version":1,"pools":[{"name":"live","members":[{"model":"p/first"}]}],'
                               '"pools":[{"name":"other","members":[{"model":"p/second"}]}]}',
        "unsupported_version": '{"version":2,"pools":[{"name":"live","members":[{"model":"p/first"}]}]}',
        "pools_not_a_list": '{"version":1,"pools":{"name":"live"}}',
        "top_level_array": '[{"name":"live","members":[{"model":"p/first"}]}]',
        "trailing_garbage": '{"version":1,"pools":[]} trailing',
    }

    def test_a_refused_document_keeps_the_pools_that_are_already_serving(self) -> None:
        for label, document in self.REFUSALS.items():
            with self.subTest(document=label), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "pools.json"
                path.write_text(self.GOOD, encoding="utf-8")
                registry = _PoolRegistry(path)
                with patch("modules.router._POOLS_STAT_INTERVAL", 0.0):
                    self.assertEqual([member.model for member in registry.get("live").members], ["p/first"])
                    path.write_text(document, encoding="utf-8")
                    self.assertEqual([member.model for member in registry.get("live").members], ["p/first"])
                    self.assertEqual(registry.names(), ["live"])

    def test_a_refused_document_is_reported_once_and_never_as_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pools.json"
            path.write_text(self.GOOD, encoding="utf-8")
            registry = _PoolRegistry(path)
            with patch("modules.router._POOLS_STAT_INTERVAL", 0.0), \
                 self.assertLogs("cx.router", level="WARNING") as logs:
                self.assertEqual(registry.names(), ["live"])
                refused = '{"version":2,"pools":[{"name":"live","members":[{"model":"p/first"}]}]}'
                path.write_text(refused + "\n" * len(refused), encoding="utf-8")
                self.assertEqual(registry.names(), ["live"])
        reported = "\n".join(logs.output)
        self.assertIn("not a supported pool document", reported)
        self.assertNotIn("ignoring it", reported)

    def test_a_duplicate_key_document_is_refused_where_the_launcher_refuses_it(self) -> None:
        for label, document in self.REFUSALS.items():
            if not label.startswith("duplicate"):
                continue
            with self.subTest(document=label), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "pools.json"
                path.write_text(document, encoding="utf-8")
                registry = _PoolRegistry(path)
                self.assertEqual(registry.names(), [])
                with self.assertRaises(RuntimeError) as refusal:
                    load_pools(path)
                self.assertIn("duplicate JSON object key", str(refusal.exception))

    def test_a_well_formed_empty_document_clears_the_pools(self) -> None:
        for label, document in (("no_pools_key", '{"version":1}'),
                                ("empty_array", '{"version":1,"pools":[]}'),
                                ("only_disabled", '{"version":1,"pools":[{"name":"live","enabled":false,'
                                                 '"members":[{"model":"p/first"}]}]}')):
            with self.subTest(document=label), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "pools.json"
                path.write_text(self.GOOD, encoding="utf-8")
                registry = _PoolRegistry(path)
                with patch("modules.router._POOLS_STAT_INTERVAL", 0.0):
                    self.assertEqual(registry.names(), ["live"])
                    path.write_text(document, encoding="utf-8")
                    self.assertEqual(registry.names(), [])

    def test_a_deleted_document_clears_the_pools(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pools.json"
            path.write_text(self.GOOD, encoding="utf-8")
            registry = _PoolRegistry(path)
            with patch("modules.router._POOLS_STAT_INTERVAL", 0.0):
                self.assertEqual(registry.names(), ["live"])
                path.unlink()
                self.assertEqual(registry.names(), [])



class EntrypointTests(unittest.TestCase):
    def test_run_forever_stops_on_configuration_errors(self) -> None:
        class Server:
            def serve_forever(self, **kwargs: object) -> None:
                pass

            def server_close(self) -> None:
                pass

        errors = config.CONFIG_ERRORS
        router_errors = router_module.CONFIG_ERRORS
        original = list(errors)
        errors[:] = ["invalid router configuration"]
        router_errors[:] = ["invalid router configuration"]
        try:
            with patch("modules.router._configure_logging"), \
                 patch("modules.router._wait_upstream") as wait, \
                 patch("modules.router._RouterServer", return_value=Server()):
                self.assertEqual(run_forever(), 1)
            wait.assert_not_called()
        finally:
            errors[:] = original
            router_errors[:] = original


class ModelCacheTests(unittest.TestCase):
    class Response:
        status = 200

        def __init__(self, body: bytes) -> None:
            self.body = body
            self.read_size = -1

        def read(self, size: int = -1) -> bytes:
            self.read_size = size
            return self.body

    class Connection:
        def __init__(self, response: ModelCacheTests.Response) -> None:
            self.response = response

        def request(self, *args: object, **kwargs: object) -> None:
            pass

        def getresponse(self) -> ModelCacheTests.Response:
            return self.response

        def close(self) -> None:
            pass

    def test_cache_reads_a_bounded_model_response(self) -> None:
        response = self.Response(b"x" * 17)
        with patch("modules.router._MAX_MODELS_RESPONSE_BYTES", 16, create=True), \
             patch("modules.router.HTTPConnection", return_value=self.Connection(response)):
            payload = _ModelListCache().get()
        self.assertEqual(payload, {"object": "list", "data": []})
        self.assertEqual(response.read_size, 17)

    def test_cache_rejects_entries_outside_the_shared_contract(self) -> None:
        # Every document is a valid list, so the entry check is the only gate here.
        empty = {"object": "list", "data": []}
        documents = (
            ("accepted", b'{"object":"list","data":[{"id":"valid"}]}', {"data": [{"id": "valid"}]}),
            ("malformed_entry", b'{"object":"list","data":[{"id":"valid"}, "invalid"]}', empty),
            ("nonfinite_constant", b'{"object":"list","data":[{"id":"valid","value":NaN}]}', empty),
            ("overlong_id", b'{"object":"list","data":[{"id":"' + b"x" * 257 + b'"}]}', empty),
            ("control_byte_in_id", b'{"object":"list","data":[{"id":"bad\\nid"}]}', empty),
        )
        for label, body, expected in documents:
            with self.subTest(entry=label):
                response = self.Response(body)
                with patch("modules.router.HTTPConnection", return_value=self.Connection(response)):
                    payload = _ModelListCache().get()
                self.assertEqual(payload, {"object": "list", **expected})

    def test_failed_cache_check_is_shared_by_concurrent_getters(self) -> None:
        cache = _ModelListCache()
        barrier, lock = threading.Barrier(2), threading.Lock()
        calls = 0

        def fetch() -> None:
            nonlocal calls
            with lock:
                calls += 1
            return None

        def get() -> dict[str, object]:
            barrier.wait()
            return cache.get()

        with patch.object(cache, "_fetch", side_effect=fetch):
            with ThreadPoolExecutor(max_workers=2) as executor:
                payloads = list(executor.map(lambda _: get(), range(2)))
        self.assertEqual(payloads, [{"object": "list", "data": []}, {"object": "list", "data": []}])
        self.assertEqual(calls, 1)

    def test_failed_cache_check_timestamp_is_recorded_after_fetch(self) -> None:
        cache = _ModelListCache()
        clock = [100.0]
        first_started, second_started, release = threading.Event(), threading.Event(), threading.Event()
        calls = 0

        def fetch() -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                first_started.set()
                self.assertTrue(release.wait(timeout=2))
            return None

        def first_get() -> dict[str, object]:
            return cache.get()

        def second_get() -> dict[str, object]:
            second_started.set()
            return cache.get()

        with patch("modules.router._now", side_effect=lambda: clock[0]), \
             patch("modules.router._MODELS_CACHE_TTL", 30.0), \
             patch.object(cache, "_fetch", side_effect=fetch), \
             ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(first_get)
            self.assertTrue(first_started.wait(timeout=2))
            clock[0] = 131.0
            second = executor.submit(second_get)
            self.assertTrue(second_started.wait(timeout=2))
            release.set()
            empty = {"object": "list", "data": []}
            self.assertEqual(first.result(timeout=2), empty)
            self.assertEqual(second.result(timeout=2), empty)
            self.assertEqual(calls, 1)
            clock[0] = 160.0
            self.assertEqual(cache.get(), empty)
            self.assertEqual(calls, 1)
            clock[0] = 161.0
            self.assertEqual(cache.get(), empty)
        self.assertEqual(calls, 2)


class _OneUpstreamModel:
    def get(self) -> dict[str, object]:
        return {"object": "list", "data": [{"id": "upstream/model", "owned_by": "vendor"}]}


class ResponseEntrypointTests(unittest.TestCase):
    def _get_models(self, router) -> tuple[int, bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", router.server_port, timeout=10)
        connection.request("GET", "/v1/models", headers={"Authorization": "Bearer test-router-key"})
        response = connection.getresponse()
        received = response.read()
        connection.close()
        return response.status, received

    def test_models_response_entrypoint_includes_pool_models(self) -> None:
        with _running_router() as router:
            router.models_cache = _OneUpstreamModel()
            status, payload = self._get_models(router)
        self.assertEqual(status, 200)
        self.assertEqual([model["id"] for model in json.loads(payload)["data"]],
                         ["upstream/model", "test-pool"])

    def test_models_response_rejects_serialized_size_over_cap(self) -> None:
        with patch("modules.router._MAX_MODELS_RESPONSE_BYTES", 100), _running_router() as router:
            router.models_cache = _OneUpstreamModel()
            status, received = self._get_models(router)
        self.assertEqual(status, 502)
        self.assertIn(b"model response too large", received)


class _FakeBodySocket:
    def __init__(self) -> None:
        self.timeout: float | None = None

    def settimeout(self, value: float) -> None:
        self.timeout = value


class TimeoutTests(unittest.TestCase):
    def test_the_upstream_socket_never_outlives_the_request_deadline(self) -> None:
        def validate(response, deadline):
            return _validate_body(
                response, "/v1/messages", _grammar_for("/v1/messages"), deadline)

        for label, exercise in (("relax_timeout", lambda r, d: r.relax_timeout(deadline=d)),
                                ("body_validation", validate)):
            with self.subTest(entry=label):
                fake = _FakeBodySocket()
                response = _UpstreamResponse(
                    200, "OK", [], iter((_OK_BODY,)), _StubConn(), body_socket=fake)
                deadline = time.monotonic() + 5.0
                self.assertIsNone(exercise(response, deadline))
                self.assertIsNotNone(fake.timeout)
                self.assertLessEqual(fake.timeout, 5.0)
                self.assertLess(fake.timeout, _UPSTREAM_TIMEOUT)


def _raw_post(router: _RouterServer, request: bytes) -> tuple[int, bytes]:
    connection = socket.create_connection(("127.0.0.1", router.server_port), timeout=10)
    try:
        connection.sendall(request)
        connection.shutdown(socket.SHUT_WR)
        response = b""
        while chunk := connection.recv(65536):
            response += chunk
    finally:
        connection.close()
    head, body = response.split(b"\r\n\r\n", 1)
    status = int(head.split(b"\r\n", 1)[0].split(b" ", 2)[1])
    length = 0
    for line in head.split(b"\r\n")[1:]:
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1])
    return status, body[:length]


def _raw_exchange(router: _RouterServer, request: bytes) -> bytes:
    connection = socket.create_connection(("127.0.0.1", router.server_port), timeout=10)
    try:
        connection.sendall(request)
        connection.shutdown(socket.SHUT_WR)
        response = b""
        while chunk := connection.recv(65536):
            response += chunk
        return response
    finally:
        connection.close()


def _raw_responses(router: _RouterServer, request: bytes) -> list[tuple[int, bytes]]:
    wire = _raw_exchange(router, request)
    responses: list[tuple[int, bytes]] = []
    while wire.startswith(b"HTTP/"):
        head, separator, wire = wire.partition(b"\r\n\r\n")
        if not separator:
            break
        lines = head.split(b"\r\n")
        status = int(lines[0].split(b" ")[1])
        length = 0
        for line in lines[1:]:
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"content-length":
                length = int(value.strip())
        responses.append((status, wire[:length]))
        wire = wire[length:]
    return responses


class _RawUpstreamHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        self.request.settimeout(10)
        data = b""
        while b"\r\n\r\n" not in data:
            part = self.request.recv(65536)
            if not part:
                return
            data += part
        head, _, rest = data.partition(b"\r\n\r\n")
        length = 0
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":", 1)[1])
        while len(rest) < length:
            part = self.request.recv(65536)
            if not part:
                break
            rest += part
        self.server.state.request_lines.append(head.split(b"\r\n", 1)[0])
        self.server.state.request_bodies.append(rest)
        if self.server.state.drip is not None:
            for delay, blob in self.server.state.drip:
                time.sleep(delay)
                try:
                    self.request.sendall(blob)
                except OSError:
                    break
            self.request.close()
            return
        self.request.sendall(self.server.state.script)
        self.request.close()


class _RawUpstreamServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


@contextmanager
def _running_raw_upstream(script: bytes = b"", drip: list[tuple[float, bytes]] | None = None,
                          members: list[dict[str, object]] | None = None):
    upstream = _RawUpstreamServer(("127.0.0.1", 0), _RawUpstreamHandler)
    upstream.state = types.SimpleNamespace(
        script=script, drip=drip, request_lines=[], request_bodies=[])
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    with tempfile.TemporaryDirectory() as directory:
        pools_file = Path(directory) / "pools.json"
        pools_file.write_text(json.dumps({"version": 1, "pools": [{
            "name": "test-pool", "strategy": "fill-first",
            "members": members or [{"model": "provider/first", "priority": 1},
                                   {"model": "provider/second", "priority": 2}]}]}),
            encoding="utf-8")
        with (patch("modules.router.PROXY_HOST", "127.0.0.1"),
              patch("modules.router.PROXY_PORT", upstream.server_address[1]),
              patch("modules.router.ROUTER_API_KEY", "test-router-key")):
            router = _RouterServer(("127.0.0.1", 0))
            router.pools = _PoolRegistry(pools_file)
            router_thread = threading.Thread(target=router.serve_forever, daemon=True)
            router_thread.start()
            try:
                yield router, upstream.state
            finally:
                router.shutdown()
                router.server_close()
                router_thread.join(timeout=3)
    upstream.shutdown()
    upstream.server_close()
    upstream_thread.join(timeout=3)


def _direct_post(target: bytes = b"/v1/completions",
                 body: bytes = b'{"model":"provider/direct","messages":[]}',
                 method: bytes = b"POST") -> bytes:
    return (method + b" " + target + b" HTTP/1.1\r\nHost: 127.0.0.1:1\r\n"
            b"Authorization: Bearer test-router-key\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)


class RequestTargetTests(unittest.TestCase):
    DIRECT_BODY = b'{"model":"provider/direct","messages":[]}'

    def _post(self, target: bytes) -> bytes:
        return (b"POST " + target + b" HTTP/1.1\r\nHost: 127.0.0.1:1\r\n"
                b"Authorization: Bearer test-router-key\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: " + str(len(self.DIRECT_BODY)).encode() + b"\r\n\r\n"
                + self.DIRECT_BODY)

    def _get(self, target: bytes) -> bytes:
        return (b"GET " + target + b" HTTP/1.1\r\nHost: 127.0.0.1:1\r\n"
                b"Authorization: Bearer test-router-key\r\n\r\n")

    def test_a_target_the_router_will_not_route_on_is_a_400_before_any_dispatch(self) -> None:
        refused = (
            ("ipv6_authority_truncated", self._get(b"http://[::1")),
            ("ipv6_authority_truncated_post", b"POST http://[::1 HTTP/1.1\r\nHost: t\r\n"
                                              b"Content-Length: 0\r\n\r\n"),
            ("non_numeric_port", self._get(b"http://127.0.0.1:not-a-port/health")),
            ("port_zero", self._get(b"http://127.0.0.1:0/health")),
            ("port_over_range", self._get(b"http://127.0.0.1:65536/health")),
            ("foreign_authority_get", self._get(b"http://example.com/health")),
            ("foreign_authority_as_authority", self._get(b"//example.com/health")),
            ("foreign_absolute_post", self._post(b"http://example.com/v1/completions")),
            ("absolute_double_slash_messages", self._post(b"http://127.0.0.1:1//x/v1/messages")),
            ("absolute_double_slash_completions", self._post(b"http://127.0.0.1:1//x/v1/completions")),
            ("absolute_triple_slash", self._post(b"http://127.0.0.1:1///v1/messages")),
            ("absolute_double_slash_query", self._post(b"http://127.0.0.1:1//v1/messages?beta=true")),
            ("absolute_double_slash_models", self._get(b"http://127.0.0.1:1//x/v1/models")),
            ("origin_double_slash_messages", self._post(b"//v1/messages")),
            ("origin_triple_slash", self._post(b"///v1/messages")),
            ("origin_double_slash_completions", self._post(b"//v1/completions")),
            ("origin_double_slash_host", self._post(b"//localhost/v1/messages")),
            ("origin_double_slash_query", self._post(b"//v1/messages?beta=true")),
            ("origin_double_slash_models", self._get(b"//v1/models")),
            ("high_byte_in_query", self._post(b"/v1/completions?x=\xff")),
            ("continuation_byte_in_query", self._post(b"/v1/completions?x=\x80")),
            ("utf8_in_query", self._post(b"/v1/completions?x=\xc3\xa9")),
            ("high_byte_in_absolute_query", self._post(b"http://127.0.0.1:1/v1/completions?x=\xff")),
            ("high_byte_in_get_query", self._get(b"/v1/completions?x=\xff")),
        )
        output = io.StringIO()
        with redirect_stderr(output):
            for label, request in refused:
                with self.subTest(target=label), _running_router((200, {}, _OK_BODY)) as router:
                    status, body = _raw_post(router, request)
                    self.assertEqual(status, 400)
                    self.assertIn(b"invalid request target", body)
                    self.assertEqual(router.upstream_state.paths, [])
                    self.assertEqual(router.upstream_state.models, [])
        self.assertNotIn("Exception occurred during processing of request", output.getvalue())
        self.assertNotIn("ValueError: Invalid IPv6 URL", output.getvalue())

    def test_local_absolute_form_is_normalized_before_forwarding(self) -> None:
        request = (
            f"POST http://127.0.0.1:{router_module.ROUTER_PORT}/v1/completions?beta=true&x=%2F HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{router_module.ROUTER_PORT}\r\n"
            f"Authorization: Bearer test-router-key\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(self.DIRECT_BODY)}\r\n\r\n"
        ).encode() + self.DIRECT_BODY
        with _running_router((200, {}, _OK_BODY)) as router:
            status, received = _raw_post(router, request)
        self.assertEqual((status, received), (200, _OK_BODY))
        self.assertEqual(router.upstream_state.paths, ["/v1/completions?beta=true&x=%2F"])
        self.assertEqual(router.upstream_state.models, ["provider/direct"])

    def test_every_accepted_target_is_forwarded_as_the_path_it_routes_on(self) -> None:
        forms = (
            (b"/v1/completions", "/v1/completions"),
            (b"/v1/completions?", "/v1/completions"),
            (b"/v1/completions?beta=true&x=%2F", "/v1/completions?beta=true&x=%2F"),
            (b"/v1/messages?a=b&c=d%2Fe", "/v1/messages?a=b&c=d%2Fe"),
            (b"http://127.0.0.1:1/v1/completions", "/v1/completions"),
            (b"http://127.0.0.1:1/v1/completions?a=b", "/v1/completions?a=b"),
            (b"http://localhost/v1/completions?a=b", "/v1/completions?a=b"),
            (b"http://[::1]/v1/completions", "/v1/completions"),
        )
        for target, routed in forms:
            with self.subTest(target=target), _running_router((200, {}, _OK_BODY)) as router:
                status, _ = _raw_post(router, self._post(target))
                self.assertEqual(status, 200)
                self.assertEqual(router.upstream_state.paths, [routed])


class _ClientProbeRecorder:
    """Records reads the router attempts on the client socket once the request
    body is already in hand."""

    def __init__(self, source: Any, probes: list[str]) -> None:
        self._source, self.probes = source, probes

    def read(self, *args: Any, **kwargs: Any) -> Any:
        self.probes.append("read")
        return self._source.read(*args, **kwargs)

    def read1(self, *args: Any, **kwargs: Any) -> Any:
        self.probes.append("read1")
        return self._source.read1(*args, **kwargs)

    def readline(self, *args: Any, **kwargs: Any) -> Any:
        self.probes.append("readline")
        return self._source.readline(*args, **kwargs)

    def peek(self, *args: Any, **kwargs: Any) -> Any:
        self.probes.append("peek")
        return self._source.peek(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._source, name)


@contextmanager
def _no_client_probe() -> Iterator[list[str]]:
    """Fail the test if the router reads the client socket after the body.

    The guard records rather than raises: socketserver swallows exceptions
    raised inside a handler thread, so a raising guard would be discarded and
    the test would pass against a router that probes.
    """
    probes: list[str] = []
    read_body = _RouterHandler._read_body

    def _read_body_then_arm(self: _RouterHandler) -> bytes | None:
        body = read_body(self)
        self.rfile = _ClientProbeRecorder(self.rfile, probes)
        return body

    with patch.object(_RouterHandler, "_read_body", _read_body_then_arm):
        yield probes


class ClientHalfCloseTests(unittest.TestCase):
    def test_failover_does_not_probe_half_closed_client_socket(self) -> None:
        body = b'{"model":"test-pool","messages":[]}'
        request = (
            b"POST /v1/messages HTTP/1.1\r\nHost: t\r\n"
            b"Authorization: Bearer test-router-key\r\n"
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
        with _no_client_probe() as probes, _running_router(
            (500, {}, b"first"), (200, {}, _OK_BODY)
        ) as router:
            status, received = _raw_post(router, request)
            models = list(router.upstream_state.models)
        self.assertEqual(probes, [], f"router probed the client socket: {probes}")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(received), json.loads(_OK_BODY))
        self.assertEqual(models, ["provider/first", "provider/second"])


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

    def test_malformed_framing_is_a_400_before_any_upstream_dispatch(self) -> None:
        head = (b"POST /v1/messages HTTP/1.1\r\nHost: t\r\n"
                b"Authorization: Bearer test-router-key\r\n")
        document = b'{"model":"test-pool","messages":[]}'
        chunked = b"Transfer-Encoding: chunked\r\n"
        cases = (
            ("short-content-length",
             b"Content-Type: application/json\r\n"
             + f"Content-Length: {len(document) + 4}\r\n".encode(), document,
             b"short Content-Length body"),
            ("noncanonical-content-length-plus", b"Content-Length: +2\r\n", b"{}",
             b"invalid Content-Length"),
            ("noncanonical-content-length-lead", b"Content-Length:  2\r\n", b"{}",
             b"invalid Content-Length"),
            ("noncanonical-content-length-trail", b"Content-Length: 2 \r\n", b"{}",
             b"invalid Content-Length"),
            ("noncanonical-content-length-underscore", b"Content-Length: 1_0\r\n", b"x" * 16,
             b"invalid Content-Length"),
            ("whitespace-before-content-length-colon", b"Content-Length : 2\r\n", b"{}",
             b"invalid Content-Length"),
            ("duplicate-content-length",
             b"Content-Length: 2\r\nContent-Length: 2\r\n", b"{}",
             b"invalid Content-Length"),
            ("content-length-with-chunked", b"Content-Length: 2\r\n" + chunked,
             b"2\r\n{}\r\n0\r\n\r\n", b"conflicting request framing"),
            ("whitespace-before-transfer-encoding-colon", b"Transfer-Encoding : chunked\r\n",
             b"2\r\n{}\r\n0\r\n\r\n", b"invalid Transfer-Encoding"),
            ("negative-chunk-size", chunked, b"-1\r\n{}\r\n0\r\n\r\n",
             b"invalid chunked framing"),
            ("noncanonical-chunk-size-plus", chunked, b"+2\r\n{}\r\n0\r\n\r\n",
             b"invalid chunked framing"),
            ("noncanonical-chunk-size-lead", chunked, b" 2\r\n{}\r\n0\r\n\r\n",
             b"invalid chunked framing"),
            ("noncanonical-chunk-size-trail", chunked, b"2 \r\n{}\r\n0\r\n\r\n",
             b"invalid chunked framing"),
            ("noncanonical-chunk-size-underscore", chunked, b"1_0\r\n" + b"x" * 16 + b"\r\n0\r\n\r\n",
             b"invalid chunked framing"),
        )
        with _running_router((200, {}, _OK_BODY)) as router:
            for name, headers, body, expected in cases:
                with self.subTest(framing=name):
                    status, received = _raw_post(router, head + headers + b"\r\n" + body)
                    self.assertEqual(status, 400)
                    self.assertIn(expected, received)
                    self.assertEqual(router.upstream_state.models, [])

    def test_chunked_trailer_bytes_are_bounded(self) -> None:
        trailers = b"".join(b"X-Trailer-%d: value\r\n" % index for index in range(8))
        request = (
            b"POST /v1/messages HTTP/1.1\r\nHost: t\r\n"
            b"Authorization: Bearer test-router-key\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n"
            b"2\r\n{}\r\n0\r\n" + trailers + b"\r\n"
        )
        with patch("modules.router._MAX_BODY_BYTES", 32), \
             _running_router((200, {}, _OK_BODY)) as router:
            status, _ = _raw_post(router, request)
        self.assertEqual(status, 413)
        self.assertEqual(router.upstream_state.models, [])


class RequestJsonBoundaryTests(unittest.TestCase):
    def _request(self, router: _RouterServer, body: bytes) -> tuple[int, bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", router.server_port, timeout=10)
        try:
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
            return response.status, response.read()
        finally:
            connection.close()

    def test_unusable_json_is_a_400_before_any_upstream_dispatch(self) -> None:
        document = b'{"model":"test-pool","messages":[],"value":'
        cases = (
            ("non-string-model", b'{"model":123,"messages":[]}'),
            ("integer-limit", b'{"model":' + b"9" * 5000 + b"}"),
            ("recursion",
             b'{"model":"test-pool","messages":' + b"[" * 10000 + b"0" + b"]" * 10000 + b"}"),
            ("nan", document + b"NaN}"),
            ("infinity", document + b"Infinity}"),
            ("negative-infinity", document + b"-Infinity}"),
        )
        with _running_router((200, {}, _OK_BODY)) as router:
            for name, body in cases:
                with self.subTest(document=name):
                    status, _ = self._request(router, body)
                    self.assertEqual(status, 400)
                    self.assertEqual(router.upstream_state.models, [])


class UpstreamJsonBoundaryTests(unittest.TestCase):
    def test_an_unusable_json_payload_tries_the_next_member(self) -> None:
        good_stream = _START_FRAME + _DELTA_FRAME + _STOP_FRAME
        cases = (
            ("body_recursion", {}, b'{"content":' + b"[" * 10000 + b"0" + b"]" * 10000 + b"}",
             _OK_BODY),
            ("body_integer_limit", {}, b'{"content":[{"value":' + b"9" * 5000 + b"}]}",
             _OK_BODY),
            ("pooled_nan", {}, b'{"content":[{"type":"text","text":"ok"}],"debug":NaN}',
             _OK_BODY),
            ("pooled_infinity", {}, b'{"content":[{"type":"text","text":"ok"}],"debug":Infinity}',
             _OK_BODY),
            ("pooled_negative_infinity", {},
             b'{"content":[{"type":"text","text":"ok"}],"debug":-Infinity}', _OK_BODY),
            ("stream_recursion", _SSE,
             b"event: message_start\ndata: " + b"[" * 10000 + b"0" + b"]" * 10000 + b"\n\n",
             good_stream),
            ("stream_integer_limit", _SSE,
             b"event: message_start\ndata: " + b"9" * 5000 + b"\n\n", good_stream),
        )
        for label, headers, bad, good in cases:
            with self.subTest(payload=label), _running_router(
                (200, headers, bad), (200, headers, good)
            ) as router:
                router.upstream_state.stream_gate.set()
                status, received = _post(router)
            self.assertEqual(status, 200)
            self.assertEqual(received, good)
            self.assertEqual(router.upstream_state.models, ["provider/first", "provider/second"])

    def test_raw_completions_passthrough_keeps_nonfinite_json_unvalidated(self) -> None:
        body = b'{"model":"provider/direct","debug":NaN}'
        with _running_router((200, {}, body)) as router:
            status, received = _post(router, "/v1/completions", model="provider/direct")
        self.assertEqual(status, 200)
        self.assertEqual(received, body)


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
                   {"model": "provider/second", "priority": 0}]
        with _running_router((200, {}, _OK_BODY), (200, {}, _OK_BODY), (200, {}, _OK_BODY),
                             members=members, strategy="round-robin") as router:
            for _ in range(3):
                self.assertEqual(_post(router)[0], 200)
        self.assertEqual(router.upstream_state.models, ["provider/first", "provider/second", "provider/first"])


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

    def test_forwarded_error_and_passthrough_bodies_use_cap(self) -> None:
        cases = (("/v1/messages", "test-pool", 400, 503),
                 ("/v1/completions", "provider/direct", 500, 502))
        for path, model, upstream_status, expected_status in cases:
            with self.subTest(path=path), patch("modules.router._MAX_RESPONSE_BYTES", 4096), _running_router(
                (upstream_status, {}, b"x" * 9999),
            ) as router:
                status, body = _post(router, path, model=model)
            self.assertEqual(status, expected_status)
            self.assertNotIn(b"x" * 100, body)
        # The refusal is a member failure, so the pool is swept rather than one
        # member's oversized body being handed on as the answer.
        with patch("modules.router._MAX_RESPONSE_BYTES", 4096), _running_router(
            *[(400, {}, b"x" * 9999)] * 4
        ) as router:
            self.assertEqual(_post(router, "/v1/messages", model="test-pool")[0], 503)
            self.assertCountEqual(
                router.upstream_state.models, ["provider/first", "provider/second"] * 2)


class DirectPathTests(unittest.TestCase):
    def test_direct_empty_stream_is_retried_then_rejected(self):
        empty = (200, _SSE, _START_FRAME + _STOP_FRAME)
        with _direct_attempts(3):
            with _running_router(empty, empty, empty) as router:
                status, body = _post(router, model="provider/direct")
        self.assertEqual(status, 502)
        self.assertIn("interrupted", body.decode())
        self.assertEqual(json.loads(body)["error"]["type"], "empty")
        self.assertEqual(router.upstream_state.models, ["provider/direct"] * 3)

    def test_direct_attempt_budget_bounds_the_retries(self):
        cases = (("empty_body", 3, (200, {}, b'{"type":"message","content":[]}')),
                 ("empty_response", 2, (200, {}, b"")))
        for label, attempts, empty in cases:
            with self.subTest(empty=label), _direct_attempts(attempts), \
                 _running_router(*[empty] * (attempts + 1)) as router:
                status, body = _post(router, model="provider/direct")
            self.assertEqual(status, 502)
            self.assertEqual(len(json.loads(body)["error"]["attempts"]), attempts)
            self.assertEqual(router.upstream_state.models, ["provider/direct"] * attempts)

    def test_direct_empty_body_recovers_on_retry(self):
        with _direct_attempts(2):
            with _running_router(
                (200, {}, b'{"type":"message","content":[]}'),
                (200, {}, _OK_BODY),
            ) as router:
                status, body = _post(router, model="provider/direct")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), json.loads(_OK_BODY))
        self.assertEqual(router.upstream_state.models, ["provider/direct"] * 2)

    def test_a_direct_attempt_the_router_rejects_lands_on_the_next_one(self):
        error_frame = b'event: error\ndata: {"type":"error","error":{"message":"boom"}}\n\n'
        good_stream = _START_FRAME + _DELTA_FRAME + _STOP_FRAME
        cases = (
            ("empty_stream", (200, _SSE, _START_FRAME + _STOP_FRAME), (200, _SSE, good_stream)),
            ("stream_error_frame", (200, _SSE, _START_FRAME + error_frame), (200, _SSE, good_stream)),
        )
        for label, rejected, good in cases:
            with self.subTest(first=label), _direct_attempts(2), _running_router(rejected, good) as router:
                status, received = _post(router, model="provider/direct")
            self.assertEqual((status, received), (200, good[2]))
            self.assertEqual(router.upstream_state.models, ["provider/direct"] * 2)

    def test_direct_valid_stream_passes_through(self):
        body = _START_FRAME + _DELTA_FRAME + _STOP_FRAME
        with _running_router((200, _SSE, body)) as router:
            status, received = _post(router, model="provider/direct")
        self.assertEqual((status, received), (200, body))
        self.assertEqual(router.upstream_state.models, ["provider/direct"])

    def test_a_direct_error_status_reaches_the_client_without_a_retry(self):
        cases = ((400, {}, b'{"error":{"message":"bad"}}'),
                 (500, {}, b'{"error":{"message":"provider down"}}'),
                 (429, {"Retry-After": "7"}, b'{"error":{"message":"slow"}}'))
        for upstream_status, headers, body in cases:
            with self.subTest(status=upstream_status), \
                 _running_router((upstream_status, headers, body)) as router:
                status, received = _post(router, model="provider/direct")
            self.assertEqual(status, upstream_status)
            self.assertEqual(received, body)
            self.assertEqual(router.upstream_state.models, ["provider/direct"])


class StopRouterTests(unittest.TestCase):
    def test_stop_router_reports_failure_when_port_remains_open(self):
        # _kill_router is not stubbed, so pid 123 must be inert for the test to be safe.
        with patch.object(router_starter, "_read_pid", return_value=123), \
             patch.object(router_starter, "_terminate_router", return_value=True), \
             patch.object(router_starter, "_stop_router_process"), \
             patch.object(router_starter, "_port_is_open", return_value=True), \
             patch.object(router_starter, "_STOP_TIMEOUT", 0.3):
            self.assertIs(router_starter.stop_router(), router_starter.StopOutcome.REFUSED)


if __name__ == "__main__":
    unittest.main()
