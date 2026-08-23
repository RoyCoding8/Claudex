from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from modules.router import _CooldownTable, _Member, _Pool, _RateLimiter, _pick_member, _sse_frame_is_error
from tests.test_router import (
    _DELTA, _OK_BODY, _SSE, _START, _UpstreamHandler, _post, _running_router,
)


class RouterHardeningTests(unittest.TestCase):
    def test_assistant_text_containing_event_error_is_not_a_stream_error(self) -> None:
        body = (
            b'event: content_block_delta\n'
            b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Here is some SSE info: event: error means failure"}}\n\n'
        )
        with _running_router((200, {"Content-Type": "text/event-stream"}, body), (200, {}, b'{"wrong":true}')) as router:
            status, received = _post(router)
        self.assertEqual(status, 200)
        self.assertEqual(received, body)
        self.assertEqual(_UpstreamHandler.models, ["provider/first"])

    def test_whole_body_in_one_flush_is_not_a_stream_error(self) -> None:
        body = b'event: message_start\ndata: {"type":"message_start"}\n\nevent: content_block_delta\ndata: {"text":"event: error"}\n\n'
        with _running_router((200, {"Content-Type": "text/event-stream"}, body)) as router:
            status, received = _post(router)
        self.assertEqual((status, received), (200, body))

    def test_later_stream_error_is_identified_by_envelope_not_message_text(self) -> None:
        self.assertFalse(_sse_frame_is_error(b'event: content_block_delta\ndata: {"delta":{"text":"event: error"}}\n\n'))
        self.assertTrue(_sse_frame_is_error(b'event: error\ndata: {"type":"error"}\n\n'))

    def test_all_members_cooling_still_attempts_every_member(self) -> None:
        members = [{"model": "provider/first", "priority": 1}, {"model": "provider/second", "priority": 2}]
        with _running_router((500, {}, b"first"), (500, {}, b"second"), members=members) as router:
            router.cooldowns.cooldown("provider/first", 30, "test")
            router.cooldowns.cooldown("provider/second", 30, "test")
            status, payload = _post(router)
        self.assertEqual(status, 503)
        self.assertEqual(len(json.loads(payload)["error"]["attempts"]), 2)
        self.assertEqual(_UpstreamHandler.models, ["provider/first", "provider/second"])

    def test_pool_larger_than_old_cap_tries_every_member(self) -> None:
        members = [{"model": f"provider/{index}", "priority": index} for index in range(10)]
        responses = [(500, {}, b"fail") for _ in members]
        with _running_router(*responses, members=members) as router:
            status, payload = _post(router)
        self.assertEqual(status, 503)
        self.assertEqual(len(json.loads(payload)["error"]["attempts"]), 10)
        self.assertEqual(_UpstreamHandler.models, [member["model"] for member in members])

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
        # Post-commit the bytes are already gone; all the router can do is
        # forward, log, and park the member so the next request avoids it.
        head = _START + _DELTA
        error = b'event: error\ndata: {"type":"error","error":{"message":"late"}}\n\n'
        with _running_router((200, _SSE, (head, error))) as router:
            _UpstreamHandler.stream_gate.set()
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
        self.assertEqual(_UpstreamHandler.models, ["provider/first", "provider/second"])

    def test_peek_socket_reset_returns_sanitized_json_error(self) -> None:
        with _running_router((0, {}, b"")) as router:
            status, received = _post(router)
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(received)["error"]["type"], "pool_exhausted")

    def test_midstream_upstream_drop_emits_final_error_event(self) -> None:
        # A drop after the commit point is unrecoverable: the client keeps the
        # partial answer plus a terminal error event, and the member is parked.
        head = _START + _DELTA
        with _running_router((200, {**_SSE, "Content-Length": "999"}, (head, b"__drop__"))) as router:
            _UpstreamHandler.stream_gate.set()
            status, received = _post(router)
            self.assertFalse(router.cooldowns.is_ready("provider/first"))
        self.assertEqual(status, 200)
        self.assertTrue(received.startswith(head))
        self.assertIn(b'event: error', received)


if __name__ == "__main__":
    unittest.main()
