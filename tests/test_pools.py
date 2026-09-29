from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from modules import pools as pools_module
from modules.models import Model, category_for
from modules.pools import (
    _LOADED_DIGESTS,
    ModelPool,
    PoolMember,
    PoolSaveConflictError,
    PoolSaveUnreadableError,
    ensure_default_pools_file,
    load_pools,
    pool_names,
    save_pools,
    validate_pools_against_models,
)


class PoolTests(unittest.TestCase):
    def tearDown(self) -> None:
        _LOADED_DIGESTS.clear()

    def test_round_trip_preserves_every_pool_and_member_field(self) -> None:
        cases = (
            ("per-member priority and rpm", [ModelPool("glm-pool", (
                PoolMember("nv/glm", rpm=40, priority=1),
                PoolMember("other/glm", rpm=50, priority=2),
            ))]),
            ("every optional field, disabled", [ModelPool(
                "weighted",
                (PoolMember("a/x", rpm=1, priority=0, limit=2, cooldown=1.5), PoolMember("b/x")),
                enabled=False, strategy="weighted",
            )]),
        )
        for name, pools in cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as directory:
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

    def test_unknown_strategy_is_coerced_with_warning(self) -> None:
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
            with self.assertLogs("cx.pools", level="WARNING"):
                pools = load_pools(path)
        self.assertEqual(len(pools), 1)
        self.assertEqual(pools[0].strategy, "fill-first")

    def test_save_rejects_an_invalid_pool_before_creating_file(self) -> None:
        cases = (
            ("duplicate pool name", ValueError, "Duplicate pool name", [
                ModelPool("duplicate", (PoolMember("a/x"), PoolMember("b/x"))),
                ModelPool("duplicate", (PoolMember("c/x"), PoolMember("d/x"))),
            ], {}),
            ("duplicate member", ValueError, "Duplicate member",
             [ModelPool("pool", (PoolMember("same/model"), PoolMember("same/model")))], {}),
            ("upstream pool collision", RuntimeError, "conflicts",
             [ModelPool("taken", (PoolMember("a/x"), PoolMember("b/x")))],
             {"upstream_models": [Model("taken", "provider")]}),
        )
        for name, error, message, pools, keywords in cases:
            with self.subTest(rejected=name), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "pools.json"
                with self.assertRaisesRegex(error, message):
                    save_pools(pools, path, **keywords)
                self.assertFalse(path.exists())

    def test_save_rejects_nonfinite_json_without_touching_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pools.json"
            original = '{"pools": []}'
            path.write_text(original, encoding="utf-8")
            load_pools(path)
            with self.assertRaises(ValueError):
                save_pools([
                    ModelPool("pool", (PoolMember("a/x", cooldown=float("nan")),)),
                ], path)
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_save_refuses_existing_file_without_loaded_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pools.json"
            original = '{"pools": [{"name": "external", "members": [{"model": "x/y"}]}]}'
            path.write_text(original, encoding="utf-8")
            with self.assertRaises(PoolSaveConflictError):
                save_pools([], path)
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_save_conflict_uses_typed_error_and_preserves_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pools.json"
            path.write_text('{"pools": []}', encoding="utf-8")
            load_pools(path)
            external = '{"pools": [{"name": "external", "members": [{"model": "x/y"}]}]}'
            path.write_text(external, encoding="utf-8")
            with self.assertRaises(PoolSaveConflictError):
                save_pools([], path)
            self.assertEqual(path.read_text(encoding="utf-8"), external)

    def test_a_load_reports_an_unadvertised_member_to_its_caller_not_the_log(self) -> None:
        upstream = [Model("a/x", "prov")]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pools.json"
            path.write_text(json.dumps({
                "version": 1,
                "pools": [{"name": "p", "members": [{"model": "a/x"}, {"model": "b/gone"}]}],
            }), encoding="utf-8")
            with patch.object(pools_module._LOG, "warning") as logged:
                pools = load_pools(path, upstream_models=upstream)

        self.assertEqual([pool.name for pool in pools], ["p"])
        self.assertEqual(
            [call for call in logged.call_args_list if "b/gone" in str(call)],
            [],
            "the load reported a mismatch its caller is going to report again",
        )
        self.assertEqual(
            validate_pools_against_models(pools, upstream),
            ["Pool 'p' references models not currently advertised by CLIProxyAPI: b/gone"],
        )

    def test_a_load_still_logs_the_entries_the_document_itself_got_wrong(self) -> None:
        upstream = [Model("provider/alpha", "provider")]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pools.json"
            path.write_text(json.dumps({
                "version": 1,
                "pools": [{"name": "p", "strategy": "random",
                           "members": [{"model": "provider/alpha"}]}],
            }), encoding="utf-8")
            with self.assertLogs("cx.pools", level="WARNING") as captured:
                load_pools(path, upstream_models=upstream)

        self.assertTrue(any("is not one of" in line for line in captured.output), captured.output)

    def test_a_launch_reports_an_unadvertised_member_exactly_once(self) -> None:
        import cx
        from modules.tui import PickerResult

        upstream = [Model("a/x", "prov")]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pools.json"
            path.write_text(json.dumps({
                "version": 1,
                "pools": [{"name": "p", "members": [{"model": "a/x"}, {"model": "b/gone"}]}],
            }), encoding="utf-8")
            printed = io.StringIO()
            with (
                patch.object(cx, "load_pools",
                             side_effect=lambda **_kwargs: load_pools(path, upstream_models=upstream)),
                patch.object(cx, "ensure_proxy"),
                patch.object(cx, "ensure_router"),
                patch.object(cx, "fetch_upstream_models", return_value=upstream),
                patch.object(cx, "fetch_models", return_value=[Model("a/x", "prov")]),
                patch.object(cx, "run_picker", return_value=PickerResult("exit", None, False, None)),
                patch.object(cx, "clear_console"),
                patch("builtins.input", return_value=""),
                patch.object(pools_module._LOG, "warning") as logged,
                redirect_stdout(printed),
            ):
                self.assertEqual(cx.main(), 0)

        self.assertEqual(printed.getvalue().count("b/gone"), 1, printed.getvalue())
        self.assertEqual(
            [call for call in logged.call_args_list if "b/gone" in str(call)],
            [],
            "the launch reported the same warning twice",
        )


    def test_crlf_load_allows_save_without_false_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pools.json"
            path.write_bytes(b'{"pools":[]}\r\n')
            load_pools(path)
            save_pools([], path)
            raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(raw, {"version": 1, "pools": []})

    def test_save_against_unreadable_file_raises_typed_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pools.json"
            path.write_text('{"pools": []}', encoding="utf-8")
            load_pools(path)
            path.write_bytes(b'\xff\xfe corrupted between load and save')
            with self.assertRaises(PoolSaveUnreadableError):
                save_pools([], path)

    def test_a_reload_of_a_stale_digest_replaces_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pools.json"
            path.write_text('{"pools": []}', encoding="utf-8")
            load_pools(path)
            canonical = path.resolve()
            self.assertIn(canonical, _LOADED_DIGESTS)
            path.write_text('{"pools": {}}', encoding="utf-8")
            load_pools(path)
            self.assertNotIn(canonical, _LOADED_DIGESTS)


if __name__ == "__main__":
    unittest.main()
