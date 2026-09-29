from __future__ import annotations

import io
import json
import os
import socket
import threading
import unittest
from contextlib import closing, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from modules.models import Model, category_for, fetch_models, fetch_models_from, fetch_upstream_models
from modules.urls import http_url, is_local_host


class ModelFetchTests(unittest.TestCase):
    @patch("modules.models._open_url")
    def test_model_request_formats_ipv6_host(self, urlopen) -> None:
        urlopen.return_value = io.BytesIO(
            b'{"object":"list","data":[{"id":"local/model","owned_by":"test"}]}'
        )

        fetch_models_from("::1", 4000, "key", "test router")

        self.assertEqual(urlopen.call_args.args[0].full_url, "http://[::1]:4000/v1/models")

    @patch("modules.models._open_url")
    def test_wire_owner_alone_never_marks_a_pool(self, urlopen) -> None:
        body = b'{"object":"list","data":[{"id":"glm-pool","owned_by":"pool"}]}'

        urlopen.return_value = io.BytesIO(body)
        self.assertEqual(
            fetch_models_from("127.0.0.1", 4000, "key", "cx router", pool_names=set()),
            [Model("glm-pool", "pool")],
        )
        self.assertEqual(
            category_for(Model("glm-pool", "pool")),
            "Custom",
        )
        urlopen.return_value = io.BytesIO(body)
        self.assertEqual(
            fetch_models_from("127.0.0.1", 4000, "key", "cx router", pool_names={"glm-pool"}),
            [Model("glm-pool", "pool", True)],
        )
        self.assertEqual(
            category_for(Model("glm-pool", "pool", True)),
            "Pools",
        )

    @patch("modules.models._open_url")
    def test_duplicate_model_ids_are_rejected_in_either_order(self, urlopen) -> None:
        first = {"id": "dup/model", "owned_by": "openai"}
        second = {"id": " dup/model ", "owned_by": "pool"}
        for records in ([first, second], [second, first]):
            with self.subTest(order=[record["owned_by"] for record in records]):
                urlopen.return_value = io.BytesIO(
                    json.dumps({"object": "list", "data": records}).encode("utf-8")
                )
                with self.assertRaisesRegex(RuntimeError, "invalid model response"):
                    fetch_models_from("127.0.0.1", 4000, "key", "test router", pool_names={"dup/model"})

    @patch("modules.models._open_url")
    def test_fetch_upstream_models_uses_proxy_boundary(self, urlopen) -> None:
        urlopen.return_value = io.BytesIO(
            b'{"object":"list","data":[{"id":"vendor/model","owned_by":"upstream"}]}'
        )

        with patch.multiple(
            "modules.models",
            PROXY_HOST="proxy.test",
            PROXY_PORT=8317,
            PROXY_API_KEY="proxy-key",
        ):
            models = fetch_upstream_models(timeout=1.25)

        self.assertEqual(models, [Model("vendor/model", "upstream")])
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://proxy.test:8317/v1/models")
        self.assertEqual(request.get_header("Authorization"), "Bearer proxy-key")
        self.assertEqual(urlopen.call_args.kwargs, {"timeout": 1.25})

    @patch("modules.models._open_url")
    def test_fetch_models_uses_router_boundary(self, urlopen) -> None:
        urlopen.return_value = io.BytesIO(
            b'{"object":"list","data":['
            b'{"id":"vendor/model","owned_by":"upstream"},'
            b'{"id":"pool/model","owned_by":"upstream"}'
            b']}'
        )

        with patch.multiple(
            "modules.models",
            ROUTER_HOST="router.test",
            ROUTER_PORT=4000,
            ROUTER_API_KEY="router-key",
        ):
            models = fetch_models(
                timeout=2.5,
                pool_names={"pool/model"},
                owner_overrides={"vendor/model": "owner"},
            )

        self.assertEqual(
            models,
            [Model("pool/model", "pool", True), Model("vendor/model", "owner")],
        )
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://router.test:4000/v1/models")
        self.assertEqual(request.get_header("Authorization"), "Bearer router-key")
        self.assertEqual(urlopen.call_args.kwargs, {"timeout": 2.5})

    @patch("modules.models._open_url")
    def test_malformed_envelopes_are_reported_as_an_invalid_model_response(self, urlopen) -> None:
        bodies = {
            "not an object": b"[]",
            "no list marker": b'{"data":[]}',
            "null model id": b'{"object":"list","data":[{"id":null,"owned_by":"openai"}]}',
            "not utf-8": b'\xff\xfe{"data":[]}',
        }
        for label, body in bodies.items():
            with self.subTest(body=label):
                urlopen.return_value = io.BytesIO(body)
                with self.assertRaisesRegex(RuntimeError, "invalid model response"):
                    fetch_models_from("127.0.0.1", 4000, "key", "test router")

    @patch("modules.models._open_url")
    def test_response_limit_accepts_exact_body_and_rejects_extra_bytes(self, urlopen) -> None:
        limit = 64
        prefix = b'{"object":"list","data":[]}'
        exact_body = prefix + b" " * (limit - len(prefix))

        with patch("modules.models._MAX_MODELS_BYTES", limit):
            urlopen.return_value = io.BytesIO(exact_body)
            self.assertEqual(fetch_models_from("127.0.0.1", 4000, "key", "test router"), [])

            urlopen.return_value = io.BytesIO(exact_body + b"x")
            with self.assertRaisesRegex(RuntimeError, "invalid model response"):
                fetch_models_from("127.0.0.1", 4000, "key", "test router")

    @patch("modules.models._open_url")
    def test_dedup_drops_a_bare_alias_only_when_a_prefixed_form_exists(self, urlopen) -> None:
        cases = (
            (
                "dropped when prefixed forms exist",
                [
                    {"id": "gpt-5.4", "owned_by": "openai"},
                    {"id": "openai/gpt-5.4", "owned_by": "openai"},
                    {"id": "vercel/openai/gpt-5.4", "owned_by": "vercel"},
                ],
                {"openai/gpt-5.4", "vercel/openai/gpt-5.4"},
            ),
            (
                "kept when no prefixed form exists",
                [{"id": "unique-model", "owned_by": "openai"}],
                {"unique-model"},
            ),
        )
        for label, data, expected in cases:
            with self.subTest(alias=label):
                urlopen.return_value = io.BytesIO(
                    json.dumps({"object": "list", "data": data}).encode("utf-8")
                )
                ids = {m.id for m in fetch_models_from("127.0.0.1", 4000, "k", "cx router")}
                self.assertEqual(ids, expected)

    @patch("modules.models._open_url")
    def test_dedup_never_hides_pool_aliases(self, urlopen) -> None:
        payload = {
            "object": "list",
            "data": [
                {"id": "glm-pool", "owned_by": "pool"},
                {"id": "nvidia/glm-pool", "owned_by": "nvidia"},
            ]
        }
        urlopen.return_value = io.BytesIO(json.dumps(payload).encode("utf-8"))
        models = fetch_models_from("127.0.0.1", 4000, "k", "cx router", pool_names={"glm-pool"})
        pool = next(m for m in models if m.id == "glm-pool")
        self.assertTrue(pool.is_pool)

    @patch("modules.models._open_url")
    def test_rejects_oversized_and_control_character_ids(self, urlopen) -> None:
        invalid_ids = ("", "   ", "x" * 257, "bad\nmodel", "bad\rmodel", "bad\tmodel")
        for model_id in invalid_ids:
            with self.subTest(model_id=model_id):
                urlopen.return_value = io.BytesIO(
                    json.dumps(
                        {"object": "list", "data": [{"id": model_id, "owned_by": "openai"}]}
                    ).encode("utf-8")
                )
                with self.assertRaisesRegex(RuntimeError, "invalid model response"):
                    fetch_models_from("127.0.0.1", 4000, "key", "test router")

    @patch("modules.models._open_url")
    def test_numbers_in_the_discarded_metadata_are_never_read(self, urlopen) -> None:
        for number in (
            "0", "-2.5", "0.5e1", "1e-400",
            "NaN", "Infinity", "-Infinity", "1e400", "-1e400", "1.5e400",
        ):
            with self.subTest(number=number):
                urlopen.return_value = io.BytesIO(
                    (
                        '{"object":"list","data":[{"id":"valid/model",'
                        f'"owned_by":"openai","metadata":{number}}}]}}'
                    ).encode()
                )
                self.assertEqual(
                    fetch_models_from("127.0.0.1", 4000, "key", "test router"),
                    [Model("valid/model", "openai")],
                )

    @patch("modules.models._open_url")
    def test_rejects_invalid_owner_metadata(self, urlopen) -> None:
        invalid_owners = (None, 17, "bad\nowner", "x" * 257)
        for owner in invalid_owners:
            with self.subTest(owner=owner):
                urlopen.return_value = io.BytesIO(
                    json.dumps(
                        {"object": "list", "data": [{"id": "valid/model", "owned_by": owner}]}
                    ).encode("utf-8")
                )
                with self.assertRaisesRegex(RuntimeError, "invalid model response"):
                    fetch_models_from("127.0.0.1", 4000, "key", "test router")

    @patch("modules.models._open_url")
    def test_accepts_router_records_without_owner_metadata(self, urlopen) -> None:
        urlopen.return_value = io.BytesIO(
            b'{"object":"list","data":[{"id":"router/model"}]}'
        )

        self.assertEqual(
            fetch_models_from("127.0.0.1", 4000, "key", "test router"),
            [Model("router/model", "")],
        )

    @patch("modules.models._open_url")
    def test_normalizes_router_model_id_before_pool_membership(self, urlopen) -> None:
        urlopen.return_value = io.BytesIO(
            b'{"object":"list","data":[{"id":" glm-pool ","owned_by":"vendor"}]}'
        )

        self.assertEqual(
            fetch_models_from(
                "127.0.0.1",
                4000,
                "key",
                "test router",
                pool_names={"glm-pool"},
            ),
            [Model("glm-pool", "pool", True)],
        )

    @patch("modules.models._open_url")
    def test_preserves_valid_model_list(self, urlopen) -> None:
        payload = {
            "object": "list",
            "data": [
                {"id": "x" * 256, "owned_by": "vendor"},
                {"id": "moonshotai/kimi-k2", "owned_by": "moonshot"},
                {"id": "openai/gpt-5", "owned_by": "openai"},
                {"id": "vendor/custom", "owned_by": "vendor"},
                {"id": "x-ai/grok-4", "owned_by": "xai"},
            ]
        }
        urlopen.return_value = io.BytesIO(json.dumps(payload).encode("utf-8"))

        models = fetch_models_from("127.0.0.1", 4000, "key", "test router")

        self.assertEqual(
            models,
            [
                Model("moonshotai/kimi-k2", "moonshot"),
                Model("openai/gpt-5", "openai"),
                Model("vendor/custom", "vendor"),
                Model("x-ai/grok-4", "xai"),
                Model("x" * 256, "vendor"),
            ],
        )

    @patch("modules.models._open_url")
    def test_deep_json_raises_invalid_model_response(self, urlopen) -> None:
        urlopen.return_value = io.BytesIO(b"[" * 10_000 + b"]" * 10_000)
        with self.assertRaisesRegex(RuntimeError, "invalid model response"):
            fetch_models_from("127.0.0.1", 4000, "key", "test router")

    @patch("modules.models._open_url")
    def test_oversized_integer_raises_invalid_model_response(self, urlopen) -> None:
        urlopen.return_value = io.BytesIO(
            b'{"object":"list","data":[{"id":' + b"9" * 5_000 + b'}]}'
        )
        with self.assertRaisesRegex(RuntimeError, "invalid model response"):
            fetch_models_from("127.0.0.1", 4000, "key", "test router")

    @patch("modules.models._open_url")
    def test_truncated_body_raises_runtime_error(self, urlopen) -> None:
        from http.client import IncompleteRead
        response = urlopen.return_value.__enter__.return_value
        response.read.side_effect = IncompleteRead(b"partial")
        with self.assertRaisesRegex(RuntimeError, "invalid model response"):
            fetch_models_from("127.0.0.1", 4000, "key", "test router")

    @patch("modules.models._open_url")
    def test_url_error_raises_not_responding(self, urlopen) -> None:
        from urllib.error import URLError
        urlopen.side_effect = URLError("connection refused")
        with self.assertRaisesRegex(RuntimeError, "not responding"):
            fetch_models_from("127.0.0.1", 4000, "key", "test router")

    def test_a_non_positive_timeout_blames_the_caller_not_the_service(self) -> None:
        with closing(socket.socket()) as probe:
            probe.bind(("127.0.0.1", 0))
            closed_port = probe.getsockname()[1]
        for timeout in (-1.0, 0.0):
            with self.subTest(timeout=timeout):
                with self.assertRaisesRegex(ValueError, "timeout must be positive"):
                    fetch_models_from("127.0.0.1", closed_port, "key", "test router", timeout=timeout)
        with self.assertRaisesRegex(RuntimeError, "not responding"):
            fetch_models_from("127.0.0.1", closed_port, "key", "test router", timeout=5.0)


class UrlTests(unittest.TestCase):
    def test_formats_a_host_for_an_http_url(self) -> None:
        cases = (
            ("::1", "http://[::1]:4000/v1/models"),
            ("[::1]", "http://[::1]:4000/v1/models"),
            ("127.0.0.1", "http://127.0.0.1:4000/v1/models"),
        )
        for host, expected in cases:
            with self.subTest(host=host):
                self.assertEqual(http_url(host, 4000, "/v1/models"), expected)


class LocalHostTests(unittest.TestCase):
    def test_classifies_local_and_remote_hosts(self) -> None:
        cases = (
            ("localhost", True),
            ("localhost.", True),
            ("LOCALHOST.", True),
            ("127.0.0.1", True),
            ("::1", True),
            ("0.0.0.0", True),
            ("::", True),
            ("::ffff:127.0.0.1", True),
            ("::ffff:0.0.0.0", True),
            ("[::ffff:127.0.0.1]", True),
            ("example.test", False),
            ("192.0.2.1", False),
            ("::ffff:192.0.2.1", False),
        )
        for host, expected in cases:
            with self.subTest(host=host):
                self.assertEqual(is_local_host(host), expected)


@contextmanager
def _serving(handler: type[BaseHTTPRequestHandler]):
    """A local HTTP server on an ephemeral port, torn down on exit."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class _QuietHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


class ProxyEnvironmentTests(unittest.TestCase):
    def test_request_url_controls_local_proxy_bypass(self) -> None:
        proxy_requests = []

        class ModelHandler(_QuietHandler):
            def do_GET(self) -> None:
                body = b'{"object":"list","data":[{"id":"local/model","owned_by":"test"}]}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        class ProxyHandler(_QuietHandler):
            def do_GET(self) -> None:
                proxy_requests.append(self.path)
                self.send_response(502)
                self.end_headers()

        with _serving(ModelHandler) as model_server, _serving(ProxyHandler) as proxy_server:
            proxy_url = f"http://127.0.0.1:{proxy_server.server_port}"
            local_url = f"http://127.0.0.1:{model_server.server_port}/v1/models"
            with patch.dict(
                os.environ,
                {
                    "HTTP_PROXY": proxy_url,
                    "ALL_PROXY": proxy_url,
                    "NO_PROXY": "not-local.invalid",
                    "no_proxy": "not-local.invalid",
                },
            ):
                models = fetch_models_from("127.0.0.1", model_server.server_port, "local-key", "test router")
                with patch("modules.models._models_url", return_value=local_url):
                    conflicting_models = fetch_models_from("example.test", 4000, "remote-key", "test router")

        self.assertEqual(models, [Model("local/model", "test")])
        self.assertEqual(conflicting_models, [Model("local/model", "test")])
        self.assertEqual(proxy_requests, [])

    def test_remote_model_discovery_keeps_environment_proxy(self) -> None:
        proxy_requests = []

        class ProxyHandler(_QuietHandler):
            def do_GET(self) -> None:
                proxy_requests.append(self.path)
                body = b'{"object":"list","data":[{"id":"remote/model","owned_by":"test"}]}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        with _serving(ProxyHandler) as proxy_server:
            proxy_url = f"http://127.0.0.1:{proxy_server.server_port}"
            with patch.dict(
                os.environ,
                {
                    "HTTP_PROXY": proxy_url,
                    "ALL_PROXY": proxy_url,
                    "NO_PROXY": "not-remote.invalid",
                    "no_proxy": "not-remote.invalid",
                },
                clear=True,
            ):
                models = fetch_models_from("example.test", 4000, "remote-key", "test router")

        self.assertEqual(models, [Model("remote/model", "test")])
        self.assertEqual(len(proxy_requests), 1)
        self.assertIn("example.test:4000/v1/models", proxy_requests[0])


if __name__ == "__main__":
    unittest.main()
