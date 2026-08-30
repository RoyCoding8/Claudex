"""The main loop must survive unexpected errors and malformed pool files (H5, H12)."""
from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import cx
from modules.models import Model
from modules.pools import load_pools
from modules.tui import PickerResult


class MainLoopTests(unittest.TestCase):
    def _run_main(self, picker):
        with ExitStack() as stack:
            for target, replacement in (
                ("ensure_proxy", lambda: None),
                ("ensure_router", lambda: None),
                ("fetch_upstream_models", lambda *a, **k: [Model("m", "prov")]),
                ("load_pools", lambda *a, **k: []),
                ("fetch_models", lambda *a, **k: [Model("m", "prov")]),
                ("run_picker", picker),
            ):
                stack.enter_context(patch.object(cx, target, replacement))
            pause = stack.enter_context(patch.object(cx, "pause_on_error"))
            stack.enter_context(patch.object(cx, "_log_traceback"))
            code = cx.main()
        return code, pause

    def test_survives_unexpected_exception_from_tui(self):
        calls = []

        def flaky_picker(*args, **kwargs):
            if not calls:
                calls.append(1)
                raise AttributeError("prompt_toolkit version drift")
            return PickerResult("exit", None, False, None)

        code, pause = self._run_main(flaky_picker)
        self.assertEqual(code, 0)
        pause.assert_called_once()
        self.assertIn("AttributeError", str(pause.call_args))

    def test_malformed_pool_file_does_not_wedge_launcher(self):
        def bad_pools(*args, **kwargs):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "pools.json"
                path.write_text('{"pools":[{"name":"x","members":"not-a-list"}]}', encoding="utf-8")
                return load_pools(path)

        with ExitStack() as stack:
            pause = stack.enter_context(patch.object(cx, "pause_on_error"))
            stack.enter_context(patch.object(cx, "_log_traceback"))
            stack.enter_context(patch.object(cx, "ensure_proxy", lambda: None))
            stack.enter_context(patch.object(cx, "ensure_router", lambda: None))
            stack.enter_context(patch.object(cx, "fetch_upstream_models", lambda *a, **k: [Model("m", "prov")]))
            stack.enter_context(patch.object(cx, "load_pools", bad_pools))
            stack.enter_context(patch.object(cx, "fetch_models", lambda *a, **k: [Model("m", "prov")]))
            stack.enter_context(patch.object(cx, "run_picker", lambda *a, **k: PickerResult("exit", None, False, None)))
            code = cx.main()
        self.assertEqual(code, 0)
        pause.assert_not_called()


if __name__ == "__main__":
    unittest.main()
