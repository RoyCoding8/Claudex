from __future__ import annotations

import io
import json
import unittest
from unittest.mock import patch

from modules.models import fetch_models_from


class ModelFetchTests(unittest.TestCase):
    @patch("modules.models.urlopen")
    def test_preserves_upstream_owner_and_marks_pool(self, urlopen) -> None:
        payload = {
            "data": [
                {"id": "nv/glm", "owned_by": "openai"},
                {"id": "glm-pool", "owned_by": "openai"},
            ]
        }
        urlopen.return_value = io.BytesIO(json.dumps(payload).encode("utf-8"))

        models = fetch_models_from(
            "127.0.0.1",
            4000,
            "key",
            "test router",
            pool_names={"glm-pool"},
            owner_overrides={"nv/glm": "nvidia"},
        )

        by_id = {model.id: model for model in models}
        self.assertEqual(by_id["nv/glm"].owner, "nvidia")
        self.assertEqual(by_id["glm-pool"].owner, "pool")
        self.assertTrue(by_id["glm-pool"].is_pool)

    @patch("modules.models.urlopen")
    def test_owner_pool_from_upstream_becomes_pool(self, urlopen) -> None:
        # The router advertises pool aliases with owned_by='pool'; fetch_models
        # should treat that as authoritative even without a pool_names hint.
        payload = {"data": [{"id": "glm-pool", "owned_by": "pool"}]}
        urlopen.return_value = io.BytesIO(json.dumps(payload).encode("utf-8"))
        models = fetch_models_from("127.0.0.1", 4000, "key", "cx router")
        self.assertTrue(models[0].is_pool)
        self.assertEqual(models[0].owner, "pool")

    @patch("modules.models.urlopen")
    def test_rejects_non_object_payload(self, urlopen) -> None:
        urlopen.return_value = io.BytesIO(b"[]")
        with self.assertRaisesRegex(RuntimeError, "invalid model response"):
            fetch_models_from("127.0.0.1", 4000, "key", "test router")

    @patch("modules.models.urlopen")
    def test_dedup_drops_bare_alias_when_prefixed_exists(self, urlopen) -> None:
        # CLIProxyAPI often serves the same model twice — once bare, once
        # prefixed. The bare form should be hidden.
        payload = {
            "data": [
                {"id": "gpt-5.4", "owned_by": "openai"},
                {"id": "openai/gpt-5.4", "owned_by": "openai"},
                {"id": "vercel/openai/gpt-5.4", "owned_by": "vercel"},
            ]
        }
        urlopen.return_value = io.BytesIO(json.dumps(payload).encode("utf-8"))
        ids = [m.id for m in fetch_models_from("127.0.0.1", 4000, "k", "cx router")]
        self.assertNotIn("gpt-5.4", ids)
        self.assertIn("openai/gpt-5.4", ids)
        self.assertIn("vercel/openai/gpt-5.4", ids)

    @patch("modules.models.urlopen")
    def test_dedup_keeps_bare_when_no_prefixed_form(self, urlopen) -> None:
        payload = {"data": [{"id": "unique-model", "owned_by": "openai"}]}
        urlopen.return_value = io.BytesIO(json.dumps(payload).encode("utf-8"))
        ids = [m.id for m in fetch_models_from("127.0.0.1", 4000, "k", "cx router")]
        self.assertIn("unique-model", ids)

    @patch("modules.models.urlopen")
    def test_dedup_never_hides_pool_aliases(self, urlopen) -> None:
        # Even if some upstream model happens to share a name with the pool,
        # the pool alias must survive.
        payload = {
            "data": [
                {"id": "glm-pool", "owned_by": "pool"},
                {"id": "nvidia/glm-pool", "owned_by": "nvidia"},
            ]
        }
        urlopen.return_value = io.BytesIO(json.dumps(payload).encode("utf-8"))
        models = fetch_models_from("127.0.0.1", 4000, "k", "cx router")
        pool = next(m for m in models if m.id == "glm-pool")
        self.assertTrue(pool.is_pool)


if __name__ == "__main__":
    unittest.main()
