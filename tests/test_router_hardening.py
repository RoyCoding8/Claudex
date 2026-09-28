from __future__ import annotations

import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modules import router
from modules.router import _CooldownTable, _Member, _pick_member, _Pool, _RateLimiter, _sse_frame_is_error
from tests.test_router import _DELTA, _OK_BODY, _SSE, _START, _STOP, _post, _running_router
from tests.test_router_regressions import _pool_passes


class RouterHardeningTests(unittest.TestCase):
    def test_assistant_text_containing_event_error_is_not_a_stream_error(self) -> None:
        body = (
            b'event: content_block_delta\n'
            b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Here is some SSE info: event: error means failure"}}\n\n'
            + _STOP
        )
        with _running_router((200, {"Content-Type": "text/event-stream"}, body), (200, {}, b'{"wrong":true}')) as router:
            status, received = _post(router)
        self.assertEqual(status, 200)
        self.assertEqual(received, body)
        self.assertEqual(router.upstream_state.models, ["provider/first"])

    def test_whole_body_in_one_flush_is_not_a_stream_error(self) -> None:
        body = b'event: message_start\ndata: {"type":"message_start"}\n\nevent: content_block_delta\ndata: {"text":"event: error"}\n\n' + _STOP
        with _running_router((200, {"Content-Type": "text/event-stream"}, body)) as router:
            status, received = _post(router)
        self.assertEqual((status, received), (200, body))

    def test_later_stream_error_is_identified_by_envelope_not_message_text(self) -> None:
        self.assertFalse(_sse_frame_is_error(b'event: content_block_delta\ndata: {"delta":{"text":"event: error"}}\n\n'))
        self.assertTrue(_sse_frame_is_error(b'event: error\ndata: {"type":"error"}\n\n'))

    def test_early_stream_error_is_not_masked_by_later_content(self) -> None:
        error = b'event: error\ndata: {"type":"error","error":{"message":"early"}}\n\n'
        good = _START + _DELTA + _STOP
        with _running_router((200, _SSE, error + _DELTA + _STOP), (200, _SSE, good)) as router:
            status, received = _post(router)
        self.assertEqual(status, 200)
        self.assertEqual(received, good)
        self.assertEqual(router.upstream_state.models, ["provider/first", "provider/second"])

    def test_early_empty_terminal_is_not_masked_by_later_content(self) -> None:
        empty = _START + _STOP + _DELTA
        good = _START + _DELTA + _STOP
        with _running_router((200, _SSE, empty), (200, _SSE, good)) as router:
            status, received = _post(router)
        self.assertEqual(status, 200)
        self.assertEqual(received, good)
        self.assertEqual(router.upstream_state.models, ["provider/first", "provider/second"])

    def test_all_members_cooling_still_attempts_every_member(self) -> None:
        members = [{"model": "provider/first", "priority": 1}, {"model": "provider/second", "priority": 2}]
        with _pool_passes(1), _running_router(
            (500, {}, b"first"), (500, {}, b"second"), members=members
        ) as router:
            router.cooldowns.cooldown("provider/first", 30, "test")
            router.cooldowns.cooldown("provider/second", 30, "test")
            status, payload = _post(router)
        self.assertEqual(status, 503)
        self.assertEqual(len(json.loads(payload)["error"]["attempts"]), 2)
        self.assertEqual(router.upstream_state.models, ["provider/first", "provider/second"])

    def test_pool_larger_than_old_cap_tries_every_member(self) -> None:
        members = [{"model": f"provider/{index}", "priority": index} for index in range(10)]
        responses = [(500, {}, b"fail") for _ in members]
        with _pool_passes(1), _running_router(*responses, members=members) as router:
            status, payload = _post(router)
        self.assertEqual(status, 503)
        self.assertEqual(len(json.loads(payload)["error"]["attempts"]), 10)
        self.assertEqual(router.upstream_state.models, [member["model"] for member in members])

    def test_non_streaming_response_has_content_length(self) -> None:
        with _running_router((200, {}, _OK_BODY)) as router:
            import http.client
            body = json.dumps({"model": "test-pool", "messages": []}).encode()
            connection = http.client.HTTPConnection("127.0.0.1", router.server_port, timeout=3)
            connection.request("POST", "/v1/messages", body=body, headers={"Authorization": "Bearer test-router-key", "Content-Length": str(len(body))})
            response = connection.getresponse()
            content_length = response.getheader("Content-Length")
            received = response.read()
            connection.close()
        self.assertEqual(content_length, str(len(received)))

    def test_priority_preserved_when_all_members_cooling(self) -> None:
        pool = _Pool("p", (_Member("first", 1, 1), _Member("second", 1, 2)))
        cooldowns = _CooldownTable()
        cooldowns.cooldown("first", 10, "test")
        cooldowns.cooldown("second", 10, "test")
        self.assertEqual(_pick_member(pool, cooldowns, set(), _RateLimiter()).model, "first")

    def test_later_stream_error_is_forwarded_and_logged(self) -> None:
        head = _START + _DELTA
        error = b'event: error\ndata: {"type":"error","error":{"message":"late"}}\n\n'
        with _running_router((200, _SSE, (head, error))) as router:
            router.upstream_state.stream_gate.set()
            with self.assertLogs("cx.router", level="WARNING") as logs:
                status, received = _post(router)
            self.assertFalse(router.cooldowns.is_ready("provider/first"))
        self.assertEqual(status, 200)
        self.assertEqual(received, head + error)
        self.assertTrue(any("trailing SSE error" in line for line in logs.output))

    def test_stream_error_before_content_never_reaches_the_client(self) -> None:
        error = b'event: error\ndata: {"type":"error","error":{"message":"early"}}\n\n'
        with _running_router((200, _SSE, _START + error), (200, {}, _OK_BODY)) as router:
            status, received = _post(router)
        self.assertEqual(status, 200)
        self.assertNotIn(b"early", received)
        self.assertEqual(router.upstream_state.models, ["provider/first", "provider/second"])

    def test_peek_socket_reset_returns_sanitized_json_error(self) -> None:
        with _pool_passes(1), _running_router((0, {}, b"")) as router:
            status, received = _post(router)
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(received)["error"]["type"], "pool_exhausted")
        self.assertEqual(router.upstream_state.models, ["provider/first", "provider/second"])

    def test_midstream_upstream_drop_emits_final_error_event(self) -> None:
        head = _START + _DELTA
        with _running_router((200, {**_SSE, "Content-Length": "999"}, (head, b"__drop__"))) as router:
            router.upstream_state.stream_gate.set()
            status, received = _post(router)
            self.assertFalse(router.cooldowns.is_ready("provider/first"))
        self.assertEqual(status, 200)
        self.assertTrue(received.startswith(head))
        self.assertIn(b'event: error', received)

    def test_crlf_sse_frames_are_forwarded_without_error(self) -> None:
        stream = b"\r\n\r\n".join([
            b"event: message_start\r\ndata: {\"type\":\"message_start\"}",
            b"event: content_block_delta\r\ndata: {\"type\":\"content_block_delta\",\"delta\":{\"text\":\"hi\"}}",
            b"event: message_stop\r\ndata: {\"type\":\"message_stop\"}",
        ]) + b"\r\n\r\n"
        with _running_router((200, _SSE, stream)) as router:
            router.upstream_state.stream_gate.set()
            status, received = _post(router)
        self.assertEqual((status, received), (200, stream))
        self.assertTrue(router.cooldowns.is_ready("provider/first"))

    def test_content_stream_without_terminal_emits_error_and_cools_member(self) -> None:
        stream = (
            b"event: content_block_delta\r\n"
            b'data: {"type":"content_block_delta","delta":{"text":"hi"}}\r\n\r\n'
        )
        with _running_router((200, _SSE, stream)) as router:
            status, received = _post(router)
        self.assertEqual(status, 200)
        self.assertTrue(received.startswith(stream))
        self.assertIn(b'event: error', received)
        self.assertEqual(router.upstream_state.models, ["provider/first"])
        self.assertFalse(router.cooldowns.is_ready("provider/first"))

    def test_undelimited_post_commit_sse_frame_is_bounded(self) -> None:
        head = _START + _DELTA
        tail = b"data: " + b"x" * 128
        with patch("modules.router._MAX_SSE_FRAME_BYTES", 128), _running_router(
            (200, _SSE, (head, tail)),
        ) as router:
            router.upstream_state.stream_gate.set()
            status, received = _post(router)
        self.assertEqual(status, 200)
        self.assertTrue(received.startswith(head))
        self.assertIn(b'event: error', received)
        self.assertEqual(router.upstream_state.models, ["provider/first"])
        self.assertFalse(router.cooldowns.is_ready("provider/first"))

    def test_oversized_pre_commit_sse_frame_fails_over_before_headers(self) -> None:
        oversized = b"data: " + b"x" * 64
        with patch("modules.router._MAX_SSE_FRAME_BYTES", 16), patch(
            "modules.router._HEAD_PEEK_BYTES", 16
        ), _running_router(
            (200, _SSE, oversized), (200, {}, _OK_BODY)
        ) as router:
            status, received = _post(router)
        self.assertEqual(status, 200)
        self.assertEqual(received, _OK_BODY)
        self.assertEqual(router.upstream_state.models, ["provider/first", "provider/second"])
        self.assertFalse(router.cooldowns.is_ready("provider/first"))

    def test_heartbeat_only_sse_beyond_head_peek_fails_over(self) -> None:
        heartbeat = b": ping\n\n" * 8
        with patch("modules.router._HEAD_PEEK_BYTES", 16), _running_router(
            (200, _SSE, (heartbeat, heartbeat)), (200, {}, _OK_BODY)
        ) as router:
            status, received = _post(router)
        self.assertEqual(status, 200)
        self.assertEqual(received, _OK_BODY)
        self.assertEqual(router.upstream_state.models, ["provider/first", "provider/second"])


if __name__ == "__main__":
    unittest.main()


class RouterLogRedactionTests(unittest.TestCase):
    def test_a_logged_value_carrying_the_api_key_never_reaches_the_log_file(self) -> None:
        root = logging.getLogger()
        previous = root.handlers
        # The handler holds router.log open; Windows cannot unlink it, so a
        # teardown failure here would mask the assertion.
        try:
            with tempfile.TemporaryDirectory() as directory:
                log = Path(directory) / "router.log"
                secret = "sk-router-secret-value"
                try:
                    with patch.object(router, "ROUTER_API_KEY", secret), \
                         patch.object(router, "ROUTER_LOG", log):
                        router._configure_logging()
                        router._LOG.warning("pool=%s", router._log_text(f"my-pool-{secret}"))
                        for handler in root.handlers:
                            handler.flush()
                    written = log.read_text(encoding="utf-8", errors="replace")
                finally:
                    for handler in root.handlers:
                        handler.close()
                    root.handlers = previous
        finally:
            root.handlers = previous

        self.assertNotIn(secret, written)
        self.assertIn("<redacted>", written)
