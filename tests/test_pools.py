from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modules.models import Model, category_for
from modules.pools import (
    ModelPool,
    PoolMember,
    ensure_default_pools_file,
    load_pools,
    pool_names,
    save_pools,
    validate_pools_against_models,
)


class PoolTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        pools = [
            ModelPool(
                "glm-pool",
                (
                    PoolMember("nv/glm", 40, None, 1),
                    PoolMember("other/glm", 50, 100000, 2),
                ),
            )
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pools.json"
            save_pools(pools, path)
            self.assertEqual(load_pools(path), pools)

    def test_default_file_copies_publishable_example(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            example = root / "pools.example.json"
            local = root / "pools.json"
            example.write_text('{"version":1,"pools":[]}\n', encoding="utf-8")
            with (
                patch("modules.pools.POOLS_EXAMPLE_FILE", example),
                patch("modules.pools.POOLS_FILE", local),
            ):
                ensure_default_pools_file()
            self.assertEqual(local.read_text(encoding="utf-8"), example.read_text(encoding="utf-8"))

    def test_pool_category(self) -> None:
        self.assertEqual(category_for(Model("glm-pool", "pool", True)), "Pools")

    def test_pool_names_returns_enabled_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pools.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "pools": [
                            {
                                "name": "on",
                                "enabled": True,
                                "members": [
                                    {"model": "a/x"},
                                    {"model": "b/x"},
                                ],
                            },
                            {
                                "name": "off",
                                "enabled": False,
                                "members": [
                                    {"model": "c/x"},
                                    {"model": "d/x"},
                                ],
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            names = pool_names(load_pools(path))
        self.assertEqual(names, {"on"})

    def test_partial_priorities_are_allowed(self) -> None:
        # The old LiteLLM-era check required either all-or-none priorities and
        # rejected ties. The router doesn't care — it uses priority as a tier
        # and rpm as tiebreaker weight.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pools.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "pools": [
                            {
                                "name": "mix",
                                "enabled": True,
                                "members": [
                                    {"model": "a/x", "priority": 1},
                                    {"model": "b/x"},
                                    {"model": "c/x", "priority": 1},
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            pools = load_pools(path)
        self.assertEqual(len(pools[0].members), 3)

    def test_rejects_ambiguous_bare_model_id(self) -> None:
        upstream = [
            Model("ollama/glm-5.2", "ollama"),
            Model("nvidia_nim/z-ai/glm-5.2", "nvidia"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pools.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "pools": [
                            {
                                "name": "bad",
                                "enabled": True,
                                "members": [
                                    {"model": "glm-5.2"},
                                    {"model": "ollama/glm-5.2"},
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "ambiguous"):
                load_pools(path, upstream_models=upstream)

    def test_strategy_round_trip(self) -> None:
        pools = [
            ModelPool("rr", (PoolMember("a/x"), PoolMember("b/x")), strategy="round-robin"),
            ModelPool("ff", (PoolMember("c/x"), PoolMember("d/x"))),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pools.json"
            save_pools(pools, path)
            self.assertEqual(load_pools(path), pools)
            raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(raw["pools"][0]["strategy"], "round-robin")
        self.assertNotIn("strategy", raw["pools"][1])

    def test_rejects_unknown_strategy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pools.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "pools": [
                            {
                                "name": "bad",
                                "members": [{"model": "a/x"}, {"model": "b/x"}],
                                "strategy": "random",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "strategy"):
                load_pools(path)

    def test_conflict_with_upstream_id_raises(self) -> None:
        pools = [ModelPool("nv/glm", (PoolMember("a/x"), PoolMember("b/x")))]
        upstream = [Model("nv/glm", "nvidia")]
        with self.assertRaisesRegex(RuntimeError, "conflicts"):
            validate_pools_against_models(pools, upstream)

    def test_missing_members_reported_as_warning(self) -> None:
        pools = [ModelPool("p", (PoolMember("a/x"), PoolMember("b/x")))]
        upstream = [Model("a/x", "prov")]
        warnings = validate_pools_against_models(pools, upstream)
        self.assertTrue(warnings and "b/x" in warnings[0])


if __name__ == "__main__":
    unittest.main()
