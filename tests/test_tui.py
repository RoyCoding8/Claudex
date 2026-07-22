from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modules.models import Model
from modules.tui import configure_model_parameters, run_picker


class _DoubleExitApplication:
    """Exercise a real picker binding against Prompt Toolkit's exit contract."""

    keys = ["Keys.ControlM"]
    invoke_twice = True

    def __init__(self, *, key_bindings, **_kwargs) -> None:
        self.key_bindings = key_bindings
        self.is_done = False
        self.result = None

    def invalidate(self) -> None:
        pass

    def exit(self, result) -> None:
        if self.is_done:
            raise Exception("Return value already set. Application.exit() failed.")
        self.is_done = True
        self.result = result

    def run(self):
        binding = next(
            binding
            for binding in self.key_bindings.bindings
            if [str(key) for key in binding.keys] == self.keys
        )
        event = type("Event", (), {"app": self})()
        binding.handler(event)
        if self.invoke_twice:
            binding.handler(event)
        return self.result


class PickerExitTests(unittest.TestCase):
    def test_repeated_launch_event_does_not_exit_application_twice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "settings.json"
            with (
                patch("modules.tui.Application", _DoubleExitApplication),
                patch("modules.tui.DATA_DIR", root),
                patch("modules.tui.SETTINGS_FILE", settings),
                patch("modules.tui.SETTINGS_EXAMPLE_FILE", root / "missing.json"),
                patch("modules.router_starter.router_is_ready", return_value=False),
            ):
                result = run_picker([Model("provider/model", "provider")])

        self.assertEqual(result.action, "launch")
        self.assertEqual(result.model.id, "provider/model")

    def test_f10_returns_selected_model_for_parameter_editing(self) -> None:
        class F10Application(_DoubleExitApplication):
            keys = ["Keys.F10"]
            invoke_twice = False

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch("modules.tui.Application", F10Application),
                patch("modules.tui.DATA_DIR", root),
                patch("modules.tui.SETTINGS_FILE", root / "settings.json"),
                patch("modules.tui.SETTINGS_EXAMPLE_FILE", root / "missing.json"),
                patch("modules.router_starter.router_is_ready", return_value=False),
            ):
                result = run_picker([Model("provider/model", "provider")])

        self.assertEqual(result.action, "model_parameters")
        self.assertEqual(result.model.id, "provider/model")

    def test_ctrl_q_binding_is_not_registered(self) -> None:
        application = None

        class CaptureApplication(_DoubleExitApplication):
            def run(self):
                nonlocal application
                application = self
                self.result = type("Result", (), {"action": "exit"})()
                return self.result

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch("modules.tui.Application", CaptureApplication),
                patch("modules.tui.DATA_DIR", root),
                patch("modules.tui.SETTINGS_FILE", root / "settings.json"),
                patch("modules.tui.SETTINGS_EXAMPLE_FILE", root / "missing.json"),
                patch("modules.router_starter.router_is_ready", return_value=False),
            ):
                run_picker([Model("provider/model", "provider")])

        keys = {
            tuple(str(key) for key in binding.keys)
            for binding in application.key_bindings.bindings
        }
        self.assertNotIn(("Keys.ControlQ",), keys)


class ModelParameterTests(unittest.TestCase):
    def test_editor_updates_selected_model_and_preserves_other_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings_file = root / "settings.json"
            settings_file.write_text(
                json.dumps(
                    {
                        "category": "Pools",
                        "model_settings": {
                            "provider/model": {"custom_parameter": "keep"},
                            "other/model": {"context_tokens": 123},
                        },
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch("modules.tui.DATA_DIR", root),
                patch("modules.tui.SETTINGS_FILE", settings_file),
                patch("modules.tui.SETTINGS_EXAMPLE_FILE", root / "missing.json"),
                patch("builtins.input", side_effect=["200,000", "off", ""]),
            ):
                configure_model_parameters("provider/model")

            saved = json.loads(settings_file.read_text(encoding="utf-8"))

        self.assertEqual(saved["category"], "Pools")
        self.assertEqual(saved["model_settings"]["other/model"]["context_tokens"], 123)
        self.assertEqual(
            saved["model_settings"]["provider/model"],
            {
                "custom_parameter": "keep",
                "context_tokens": 200000,
                "auto_compact": False,
            },
        )

    def test_editor_clear_removes_empty_model_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings_file = root / "settings.json"
            settings_file.write_text(
                json.dumps(
                    {
                        "model_settings": {
                            "provider/model": {
                                "context_tokens": 200000,
                                "auto_compact": True,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch("modules.tui.DATA_DIR", root),
                patch("modules.tui.SETTINGS_FILE", settings_file),
                patch("modules.tui.SETTINGS_EXAMPLE_FILE", root / "missing.json"),
                patch("builtins.input", side_effect=["clear", "default", ""]),
            ):
                configure_model_parameters("provider/model")

            saved = json.loads(settings_file.read_text(encoding="utf-8"))

        self.assertNotIn("provider/model", saved["model_settings"])


if __name__ == "__main__":
    unittest.main()
