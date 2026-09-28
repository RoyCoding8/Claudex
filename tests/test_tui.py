from __future__ import annotations

import io
import json
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from prompt_toolkit import Application as PromptToolkitApplication
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

from modules.models import Model, filter_models
from modules.tui import configure_model_parameters, run_picker


class _DoubleExitApplication:
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


def _run_real_picker(
    input_text: str,
    settings: dict | None = None,
    models: list[Model] | None = None,
):
    captured: list[PromptToolkitApplication] = []
    result: list[object] = []
    errors: list[BaseException] = []
    real_application = PromptToolkitApplication

    def capture_application(**kwargs):
        application = real_application(input=pipe_input, output=DummyOutput(), **kwargs)
        captured.append(application)
        return application

    if models is None:
        models = [Model("openai/gpt-5", "openai")]

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        settings_file = root / "settings.json"
        if settings is not None:
            settings_file.write_text(json.dumps(settings), encoding="utf-8")
        with (
            patch("modules.tui.Application", side_effect=capture_application),
            patch("modules.tui.DATA_DIR", root),
            patch("modules.tui.SETTINGS_FILE", settings_file),
            patch("modules.tui.SETTINGS_EXAMPLE_FILE", root / "missing.json"),
            patch("modules.router_starter.router_is_ready", return_value=False),
            create_pipe_input() as pipe_input,
        ):
            def run() -> None:
                try:
                    result.append(run_picker(models))
                except BaseException as error:
                    errors.append(error)

            thread = threading.Thread(target=run)
            thread.start()
            pipe_input.send_text(input_text)

            construction_budget = 10.0
            started = time.monotonic()
            construction_deadline = started + construction_budget
            while not captured and time.monotonic() < construction_deadline:
                time.sleep(0.01)
            constructed = time.monotonic()
            if not captured:
                thread.join(0)
                raise AssertionError(
                    f"the picker never constructed an Application within {construction_budget:.1f}s"
                    f" (waited {constructed - started:.2f}s)"
                )

            exit_budget = 10.0
            injected = time.monotonic()
            thread.join(exit_budget)
            timed_out = thread.is_alive()
            if timed_out:
                captured[0].exit()
                thread.join(exit_budget)
                timed_out = thread.is_alive()
            waited = time.monotonic() - injected

    if timed_out:
        raise AssertionError(
            f"the picker did not exit within {exit_budget:.1f}s of the injected keystroke"
            f" (waited {waited:.2f}s, construction took {constructed - started:.2f}s)"
        )
    if errors:
        raise errors[0]
    return result[0]


class PickerExitTests(unittest.TestCase):
    def test_a_launch_key_reports_its_own_action_exactly_once(self) -> None:
        class F10Application(_DoubleExitApplication):
            keys = ["Keys.F10"]
            invoke_twice = False

        cases = (
            ("Enter pressed twice", _DoubleExitApplication, "launch"),
            ("F10 pressed once", F10Application, "model_parameters"),
        )
        for name, application, action in cases:
            with self.subTest(binding=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with (
                    patch("modules.tui.Application", application),
                    patch("modules.tui.DATA_DIR", root),
                    patch("modules.tui.SETTINGS_FILE", root / "settings.json"),
                    patch("modules.tui.SETTINGS_EXAMPLE_FILE", root / "missing.json"),
                    patch("modules.router_starter.router_is_ready", return_value=False),
                ):
                    result = run_picker([Model("provider/model", "provider")])

                self.assertEqual(result.action, action)
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
        self.assertIn(("Keys.ControlM",), keys)
        self.assertNotIn(("Keys.ControlQ",), keys)

    def test_the_key_that_leaves_the_picker_exits_it(self) -> None:
        for key, name in (("\x03", "Ctrl-C"), ("\x04", "Ctrl-D"), ("\x1b", "Escape")):
            with self.subTest(key=name):
                self.assertEqual(_run_real_picker(key).action, "exit")

    def test_real_picker_search_selects_exact_matching_model(self) -> None:
        result = _run_real_picker(
            "gpt\r",
            models=[
                Model("xai/grok-4", "xai"),
                Model("openai/gpt-5", "openai"),
                Model("pool/shared", "pool", True),
            ],
        )

        self.assertEqual(result.action, "launch")
        self.assertEqual(result.model.id, "openai/gpt-5")

    def test_string_false_is_not_enabled_as_skip_permissions(self) -> None:
        result = _run_real_picker("\x04", {"skip_permissions": "false"})

        self.assertFalse(result.skip_permissions)

    def test_malformed_model_roles_render_as_defaults(self) -> None:
        result = _run_real_picker(
            "\x04",
            {"gpt_fast_model": 7, "gpt_medium_model": [], "gpt_subagent_model": {}},
        )

        self.assertEqual(
            (result.gpt_fast_model, result.gpt_medium_model, result.gpt_subagent_model),
            (None, None, None),
        )

    def test_open_picker_returns_roles_changed_externally(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings_file = root / "settings.json"
            settings_file.write_text(
                json.dumps(
                    {
                        "gpt_fast_model": "old/fast",
                        "gpt_medium_model": "old/medium",
                        "gpt_subagent_model": "old/subagent",
                        "unrelated": "keep",
                    }
                ),
                encoding="utf-8",
            )

            class ExternalChangeApplication(_DoubleExitApplication):
                keys = ["Keys.ControlM"]
                invoke_twice = False

                def run(self):
                    settings_file.write_text(
                        json.dumps(
                            {
                                "gpt_fast_model": "new/fast",
                                "gpt_medium_model": "new/medium",
                                "gpt_subagent_model": "new/subagent",
                                "unrelated": "keep",
                            }
                        ),
                        encoding="utf-8",
                    )
                    return super().run()

            with (
                patch("modules.tui.Application", ExternalChangeApplication),
                patch("modules.tui.DATA_DIR", root),
                patch("modules.tui.SETTINGS_FILE", settings_file),
                patch("modules.tui.SETTINGS_EXAMPLE_FILE", root / "missing.json"),
                patch("modules.router_starter.router_is_ready", return_value=False),
            ):
                result = run_picker([Model("provider/model", "provider")])

            saved = json.loads(settings_file.read_text(encoding="utf-8"))

        self.assertEqual(
            (result.gpt_fast_model, result.gpt_medium_model, result.gpt_subagent_model),
            ("new/fast", "new/medium", "new/subagent"),
        )
        self.assertEqual(
            saved,
            {
                "gpt_fast_model": "new/fast",
                "gpt_medium_model": "new/medium",
                "gpt_subagent_model": "new/subagent",
                "unrelated": "keep",
                "last_model": "provider/model",
            },
        )


class ModelFilterTests(unittest.TestCase):
    def test_filter_models_returns_exact_category_and_query_matches(self) -> None:
        models = [
            Model("openai/gpt-5", "openai"),
            Model("xai/grok-4", "xai"),
            Model("pool/shared", "pool", True),
            Model("kimi/k2", "moonshot"),
            Model("vendor/custom", "vendor"),
        ]

        self.assertEqual(
            [model.id for model in filter_models(models, "Codex", "gpt")],
            ["openai/gpt-5"],
        )
        self.assertEqual(
            [model.id for model in filter_models(models, "Pools", "")],
            ["pool/shared"],
        )
        self.assertEqual(
            [model.id for model in filter_models(models, "All", "vendor custom")],
            ["vendor/custom"],
        )


class ModelParameterTests(unittest.TestCase):
    def test_editor_updates_selected_model_and_preserves_other_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings_file = root / "settings.json"
            settings_file.write_text(
                json.dumps(
                    {
                        "category": "Pools",
                        "gpt_medium_model": "other/provider",
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
        self.assertEqual(saved["gpt_medium_model"], "other/provider")
        self.assertEqual(saved["model_settings"]["other/model"]["context_tokens"], 123)
        self.assertEqual(
            saved["model_settings"]["provider/model"],
            {
                "custom_parameter": "keep",
                "context_tokens": 200000,
                "auto_compact": False,
            },
        )

    def test_editor_reloads_unrelated_settings_after_prompting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings_file = root / "settings.json"
            settings_file.write_text(
                json.dumps(
                    {
                        "category": "Pools",
                        "model_settings": {
                            "provider/model": {"custom_parameter": "keep"},
                        },
                    }
                ),
                encoding="utf-8",
            )

            def respond(prompt):
                if prompt.startswith("Context size"):
                    current = json.loads(settings_file.read_text(encoding="utf-8"))
                    current["gpt_medium_model"] = "external/provider"
                    settings_file.write_text(json.dumps(current), encoding="utf-8")
                    return "200,000"
                if prompt.startswith("Auto-compact"):
                    return "off"
                return ""

            with (
                patch("modules.tui.DATA_DIR", root),
                patch("modules.tui.SETTINGS_FILE", settings_file),
                patch("modules.tui.SETTINGS_EXAMPLE_FILE", root / "missing.json"),
                patch("builtins.input", side_effect=respond),
            ):
                configure_model_parameters("provider/model")

            saved = json.loads(settings_file.read_text(encoding="utf-8"))

        self.assertEqual(saved["category"], "Pools")
        self.assertEqual(saved["gpt_medium_model"], "external/provider")
        self.assertEqual(saved["model_settings"]["provider/model"]["custom_parameter"], "keep")
        self.assertEqual(saved["model_settings"]["provider/model"]["context_tokens"], 200000)

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

    def test_editor_prompt_interruptions_cancel_without_traceback(self) -> None:
        for error in (EOFError(), KeyboardInterrupt()):
            for prompt_index, inputs in enumerate(([error], ["200000", error], ["200000", "off", error])):
                with self.subTest(error=type(error).__name__, prompt_index=prompt_index):
                    with tempfile.TemporaryDirectory() as directory:
                        root = Path(directory)
                        settings_file = root / "settings.json"
                        original = {"model_settings": {"provider/model": {"context_tokens": 100}}}
                        settings_file.write_text(json.dumps(original), encoding="utf-8")
                        with (
                            patch("modules.tui.DATA_DIR", root),
                            patch("modules.tui.SETTINGS_FILE", settings_file),
                            patch("modules.tui.SETTINGS_EXAMPLE_FILE", root / "missing.json"),
                            patch("builtins.input", side_effect=inputs),
                        ):
                            configure_model_parameters("provider/model")

                        saved = json.loads(settings_file.read_text(encoding="utf-8"))

                    if prompt_index < 2:
                        self.assertEqual(saved, original)
                    else:
                        self.assertEqual(saved["model_settings"]["provider/model"]["context_tokens"], 200000)
                        self.assertIs(saved["model_settings"]["provider/model"]["auto_compact"], False)


    def test_keep_leaves_a_value_edited_elsewhere_alone_and_says_so(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings_file = root / "settings.json"
            settings_file.write_text(
                json.dumps({"model_settings": {"provider/model": {"context_tokens": 100000}}}),
                encoding="utf-8",
            )
            printed = io.StringIO()

            def respond(prompt):
                if prompt.startswith("Context size"):
                    current = json.loads(settings_file.read_text(encoding="utf-8"))
                    current["model_settings"]["provider/model"]["context_tokens"] = 250000
                    settings_file.write_text(json.dumps(current), encoding="utf-8")
                return ""

            with (
                patch("modules.tui.SETTINGS_FILE", settings_file),
                patch("modules.tui.DATA_DIR", root),
                patch("modules.tui.SETTINGS_EXAMPLE_FILE", root / "missing.json"),
                patch("builtins.input", side_effect=respond),
                redirect_stdout(printed),
            ):
                configure_model_parameters("provider/model")

            saved = json.loads(settings_file.read_text(encoding="utf-8"))
            output = printed.getvalue()

        self.assertEqual(saved["model_settings"]["provider/model"], {"context_tokens": 250000})
        self.assertIn("context_tokens changed on disk while Claudex was open", output)
        self.assertIn("the value on this screen was not written", output)
        self.assertNotIn("Saved to", output)

    def test_a_save_refused_while_prompting_is_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings_file = root / "settings.json"
            original = json.dumps({"model_settings": {"provider/model": {}}})
            settings_file.write_text(original, encoding="utf-8")
            printed = io.StringIO()
            answers = iter(["200000", "off", ""])

            def respond(prompt):
                if prompt.startswith("Context size"):
                    settings_file.write_text(
                        json.dumps(
                            {
                                "model_settings": {"provider/model": {}},
                                "padding": "y" * 128,
                            }
                        ),
                        encoding="utf-8",
                    )
                return next(answers)

            with (
                patch("modules.tui.SETTINGS_FILE", settings_file),
                patch("modules.tui.DATA_DIR", root),
                patch("modules.tui.SETTINGS_EXAMPLE_FILE", root / "missing.json"),
                patch("modules.tui._MAX_SETTINGS_BYTES", 96),
                patch("builtins.input", side_effect=respond),
                redirect_stdout(printed),
            ):
                configure_model_parameters("provider/model")

            source = settings_file.read_text(encoding="utf-8")
            output = printed.getvalue()

        self.assertNotIn("Saved to", output)
        self.assertIn("not saved: settings.json is not the file Claudex last read", output)
        self.assertIn('"padding": "' + "y" * 128 + '"', source)


if __name__ == "__main__":
    unittest.main()
