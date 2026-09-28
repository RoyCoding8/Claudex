"""The main loop survives unexpected errors and malformed pool files."""
from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import cx
from modules.config import ConfigError, ConfigSource
from modules.models import Model
from modules.pools import ModelPool, PoolMember, PoolSaveRecoveryError, load_pools
from modules.router_starter import StopOutcome
from modules.tui import PickerResult

_POOLS_THEN_EXIT = (
    PickerResult("pools", None, False, None),
    PickerResult("exit", None, False, None),
)


class MainLoopTests(unittest.TestCase):
    @staticmethod
    def _patched(stack, picker, model="m", **overrides):
        """Inert stand-ins for the main loop's collaborators; `overrides` replaces any target."""
        offered = Model(model, "prov") if isinstance(model, str) else model
        replacements = {
            "ensure_proxy": lambda: None,
            "ensure_router": lambda: None,
            "fetch_upstream_models": lambda *a, **k: [offered],
            "load_pools": lambda *a, **k: [],
            "fetch_models": lambda *a, **k: [offered],
            "_log_traceback": lambda error: None,
            "run_picker": picker,
        }
        for target, replacement in {**replacements, **overrides}.items():
            stack.enter_context(patch.object(cx, target, replacement))

    def _run_main(self, picker, model="m", **overrides):
        with ExitStack() as stack:
            self._patched(stack, picker, model, **overrides)
            pause = stack.enter_context(patch.object(cx, "pause_on_error"))
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

    def test_launch_dispatches_selected_model_and_exact_cli_arguments(self):
        model = Model("pool/name", "owner")
        picks = iter([
            PickerResult("launch", model, True, 100000, True, "fast", "medium", "subagent"),
            PickerResult("exit", None, False, None),
        ])

        with ExitStack() as stack:
            self._patched(
                stack,
                lambda *args, **kwargs: next(picks),
                model,
                clear_console=lambda: None,
            )
            launch = stack.enter_context(patch.object(cx, "launch_claude", return_value=0))
            stack.enter_context(patch.object(sys, "argv", ["cx.py", "", "--flag=a&b"]))

            self.assertEqual(cx.main(), 0)

        self.assertEqual(
            launch.call_args.args,
            ("pool/name", True, 100000, True, ["", "--flag=a&b"], "fast", "medium", "subagent"),
        )
        self.assertEqual(launch.call_args.kwargs, {"openai_family": False})

    def test_pool_management_action_returns_to_picker(self):
        model = Model("m", "prov")
        picks = iter(_POOLS_THEN_EXIT)
        opened = []

        def open_manager(models):
            opened.append(models)

        with patch.object(cx, "run_pool_manager", open_manager):
            code, _pause = self._run_main(lambda *args, **kwargs: next(picks))

        self.assertEqual(code, 0)
        self.assertEqual(opened, [[model]])

    def test_pool_save_recovery_error_preserves_pending_edits(self):
        pending = [ModelPool("pending", (PoolMember("m"),))]
        picks = iter(_POOLS_THEN_EXIT)

        def picker(*args, **kwargs):
            try:
                return next(picks)
            except StopIteration:
                raise KeyboardInterrupt from None

        with tempfile.TemporaryDirectory() as directory, \
             patch("modules.pool_tui.POOLS_FILE", Path(directory) / "pools.json"), \
             patch.object(cx, "run_pool_manager", side_effect=PoolSaveRecoveryError(pending, "artifact write failed")), \
             patch.object(cx, "clear_console"), \
             patch("builtins.input", return_value="") as prompt:
            code, pause = self._run_main(picker)

            self.assertEqual(code, 0)
            pause.assert_not_called()
            prompt.assert_called_once_with("\nPress Enter to write them to a pool recovery file...")
            self.assertEqual(
                load_pools(Path(directory) / "pools.conflict.json"),
                pending,
            )

    def test_pool_recovery_is_attempted_once_when_the_recovery_write_fails(self):
        picks = iter(_POOLS_THEN_EXIT)
        writes = []

        def failing_write(pools, upstream_models):
            writes.append(list(pools))
            if len(writes) == 1:
                raise OSError("disk full")

        with patch.object(cx, "run_pool_manager", side_effect=PoolSaveRecoveryError([], "artifact write failed")), \
             patch.object(cx, "_write_pools_conflict", failing_write), \
             patch.object(cx, "clear_console"):
            code, pause = self._run_main(lambda *args, **kwargs: next(picks))

        self.assertEqual(code, 0)
        self.assertEqual(len(writes), 1)
        pause.assert_called_once()

    def test_interrupted_pool_recovery_write_states_the_unsaved_edits_and_exits(self):
        pending = [ModelPool("pending", (PoolMember("m"),))]
        picks = iter(_POOLS_THEN_EXIT)

        with tempfile.TemporaryDirectory() as directory, \
             patch("modules.pool_tui.POOLS_FILE", Path(directory) / "pools.json"), \
             patch("modules.pool_tui.reserve_pools_conflict_path", side_effect=KeyboardInterrupt), \
             patch.object(cx, "run_pool_manager", side_effect=PoolSaveRecoveryError(pending, "artifact write failed")), \
             patch.object(cx, "clear_console"), \
             patch("builtins.input", return_value=""), \
             patch("builtins.print") as output:
            code, pause = self._run_main(lambda *args, **kwargs: next(picks))
            written = sorted(path.name for path in Path(directory).iterdir())

        self.assertEqual(code, 130)
        pause.assert_not_called()
        self.assertEqual(written, [])
        printed = " ".join(str(call) for call in output.call_args_list)
        self.assertIn("No pool recovery file was written", printed)
        self.assertIn("pending", printed)

    def test_recovery_writes_the_file_when_no_prompt_can_be_answered(self):
        pending = [ModelPool("pending", (PoolMember("m"),))]
        picks = iter(_POOLS_THEN_EXIT)

        with tempfile.TemporaryDirectory() as directory, \
             patch("modules.pool_tui.POOLS_FILE", Path(directory) / "pools.json"), \
             patch.object(cx, "run_pool_manager", side_effect=PoolSaveRecoveryError(pending, "artifact write failed")), \
             patch.object(cx, "clear_console"), \
             patch.object(sys, "stdin", None), \
             patch("builtins.print") as output:
            code, pause = self._run_main(lambda *args, **kwargs: next(picks))

            self.assertEqual(code, 0)
            pause.assert_not_called()
            self.assertEqual(load_pools(Path(directory) / "pools.conflict.json"), pending)

        printed = " ".join(str(call) for call in output.call_args_list)
        self.assertIn("writing the recovery file", printed)

    def test_failed_recovery_write_reports_the_unsaved_edits_for_a_full_disk(self):
        pending = [ModelPool("pending", (PoolMember("m"),))]
        picks = iter(_POOLS_THEN_EXIT)

        with tempfile.TemporaryDirectory() as directory, \
             patch("modules.pool_tui.POOLS_FILE", Path(directory) / "pools.json"), \
             patch("modules.pool_tui.save_pools", side_effect=OSError(28, "No space left on device")), \
             patch.object(cx, "run_pool_manager", side_effect=PoolSaveRecoveryError(pending, "artifact write failed")), \
             patch.object(cx, "clear_console"), \
             patch("builtins.input", return_value=""), \
             patch("builtins.print") as output:
            code, pause = self._run_main(lambda *args, **kwargs: next(picks))
            written = (Path(directory) / "pools.conflict.json").exists()

        self.assertEqual(code, 130)
        self.assertFalse(written)
        pause.assert_not_called()
        printed = " ".join(str(call) for call in output.call_args_list)
        self.assertIn("No space left on device", printed)
        self.assertIn("Unsaved pool edits: pending", printed)

    def test_pool_recovery_write_survives_ctrl_c_at_the_prompt(self):
        pending = [ModelPool("pending", (PoolMember("m"),))]
        picks = iter(_POOLS_THEN_EXIT)

        with tempfile.TemporaryDirectory() as directory, \
             patch("modules.pool_tui.POOLS_FILE", Path(directory) / "pools.json"), \
             patch.object(cx, "run_pool_manager", side_effect=PoolSaveRecoveryError(pending, "artifact write failed")), \
             patch.object(cx, "clear_console"), \
             patch("builtins.input", side_effect=KeyboardInterrupt):
            code, pause = self._run_main(lambda *args, **kwargs: next(picks))

            self.assertEqual(code, 0)
            pause.assert_not_called()
            self.assertEqual(
                load_pools(Path(directory) / "pools.conflict.json"),
                pending,
            )

    def test_picker_raising_keyboard_interrupt_or_eof_exits_130(self):
        for raised in (KeyboardInterrupt, EOFError):
            with self.subTest(raised=raised.__name__):
                picks = []

                def picker(*args, _raised=raised, _picks=picks, **kwargs):
                    _picks.append(1)
                    if len(_picks) > 1:
                        return PickerResult("exit", None, False, None)
                    raise _raised

                with ExitStack() as stack:
                    self._patched(stack, picker, clear_console=lambda: None)
                    stack.enter_context(patch("builtins.input", return_value=""))
                    output = stack.enter_context(patch("builtins.print"))
                    self.assertEqual(cx.main(), 130)

                self.assertEqual(len(picks), 1)
                printed = " ".join(str(call) for call in output.call_args_list)
                self.assertNotIn("could not refresh", printed)

    def test_a_dead_terminal_at_the_post_launch_prompt_exits_130(self):
        for closed_stdin in (False, True):
            with self.subTest(closed_stdin=closed_stdin):
                model = Model("m", "prov")
                picks = iter([
                    PickerResult("launch", model, False, None, None),
                    PickerResult("exit", None, False, None),
                ])

                with ExitStack() as stack:
                    self._patched(
                        stack,
                        lambda *args, _picks=picks, **kwargs: next(_picks),
                        launch_claude=lambda *a, **k: 7,
                        clear_console=lambda: None,
                    )
                    if closed_stdin:
                        stack.enter_context(patch.object(sys, "stdin", None))
                    else:
                        stack.enter_context(patch("builtins.input", side_effect=[EOFError, ""]))
                    output = stack.enter_context(patch("builtins.print"))
                    self.assertEqual(cx.main(), 130)

                printed = " ".join(str(call) for call in output.call_args_list)
                self.assertNotIn("could not refresh", printed)

    def test_closed_stdin_at_the_retry_prompt_exits_instead_of_prompting_again(self):
        prompts = []

        def no_terminal_picker(*args, **kwargs):
            prompts.append(1)
            if len(prompts) > 1:
                return PickerResult("exit", None, False, None)
            raise RuntimeError("input(): lost sys.stdin")

        with ExitStack() as stack:
            self._patched(stack, no_terminal_picker, clear_console=lambda: None)
            stack.enter_context(patch.object(sys, "stdin", None))
            output = stack.enter_context(patch("builtins.print"))
            self.assertEqual(cx.main(), 130)

        self.assertEqual(len(prompts), 1)
        printed = " ".join(str(call) for call in output.call_args_list)
        self.assertEqual(printed.count("could not refresh"), 1)

    def test_configure_action_reopens_picker_after_updating_roles(self):
        model = Model("m", "prov")
        picks = iter([
            PickerResult("configure", None, False, None),
            PickerResult("exit", None, False, None),
        ])
        configured = []

        def configure(model_list):
            configured.append(model_list)

        with patch.object(cx, "_configure_extra_models", configure):
            code, _pause = self._run_main(lambda *args, **kwargs: next(picks))

        self.assertEqual(code, 0)
        self.assertEqual(configured, [[model]])

    def test_nonzero_launch_returns_to_picker_and_waits_before_it_reopens(self):
        model = Model("m", "prov")
        picks = iter([
            PickerResult("launch", model, False, None, None),
            PickerResult("exit", None, False, None),
        ])

        with ExitStack() as stack:
            self._patched(
                stack,
                lambda *args, **kwargs: next(picks),
                model,
                launch_claude=lambda *a, **k: 7,
                clear_console=lambda: None,
            )
            output = stack.enter_context(patch("builtins.print"))
            prompt = stack.enter_context(patch("builtins.input", return_value=""))

            self.assertEqual(cx.main(), 0)

        prompt.assert_called_once_with("Press Enter to return to Claudex...")
        output.assert_any_call("\nClaude Code exited with code 7.")

    def test_an_interrupted_claude_code_is_not_reported_as_a_failure(self):
        model = Model("m", "prov")
        prompted = []

        for exit_code in (130, -2, -1073741510, 3221225786):
            with self.subTest(exit_code=exit_code):
                picks = iter([
                    PickerResult("launch", model, False, None, None),
                    PickerResult("exit", None, False, None),
                ])
                prompted.clear()

                def picker(*args, _picks=picks, **kwargs):
                    return next(_picks)

                def launch(*args, _code=exit_code, **kwargs):
                    return _code

                with ExitStack() as stack:
                    self._patched(
                        stack,
                        picker,
                        launch_claude=launch,
                        clear_console=lambda: None,
                    )
                    output = stack.enter_context(patch("builtins.print"))
                    stack.enter_context(patch("builtins.input", side_effect=lambda prompt: prompted.append(prompt) or ""))

                    self.assertEqual(cx.main(), 0)

                printed = " ".join(str(call) for call in output.call_args_list)
                self.assertNotIn("exited with code", printed)
                self.assertEqual(prompted, [])

    def test_malformed_pool_file_does_not_wedge_launcher(self):
        def bad_pools(*args, **kwargs):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "pools.json"
                path.write_text('{"pools":[{"name":"x","members":"not-a-list"}]}', encoding="utf-8")
                return load_pools(path)

        with ExitStack() as stack:
            pause = stack.enter_context(patch.object(cx, "pause_on_error"))
            self._patched(
                stack,
                lambda *a, **k: PickerResult("exit", None, False, None),
                load_pools=bad_pools,
            )
            code = cx.main()
        self.assertEqual(code, 0)
        pause.assert_not_called()


class ManagementUrlTests(unittest.TestCase):
    @patch("cx._ask", return_value="")
    @patch("cx.webbrowser.open", return_value=True)
    @patch("cx.clear_console")
    def test_management_url_formats_ipv6_proxy_host(self, _clear_console, open_browser, _ask) -> None:
        with patch("modules.config.PROXY_HOST", "::1"), patch("modules.config.PROXY_PORT", 8317):
            cx._open_management()

        self.assertEqual(open_browser.call_args.args, ("http://[::1]:8317/management.html",))


class StartupDiagnosticTests(unittest.TestCase):
    def test_default_config_error_uses_neutral_header_and_stops_before_proxy(self):
        errors = [ConfigError("proxy key is invalid", ConfigSource.DEFAULT)]
        with patch.object(cx, "CONFIG_ERRORS", errors), \
             patch.object(cx, "ensure_proxy") as ensure_proxy, \
             patch("builtins.print") as output:
            self.assertEqual(cx.main(), 1)

        ensure_proxy.assert_not_called()
        messages = [call.args[0] for call in output.call_args_list if call.args]
        self.assertIn("Configuration problems:", messages)
        self.assertNotIn("Configuration problems in .env:", messages)

    def test_mixed_file_and_process_errors_name_env_file(self):
        errors = [
            ConfigError("file setting is invalid", ConfigSource.FILE),
            ConfigError("process setting is invalid", ConfigSource.PROCESS),
        ]
        with patch.object(cx, "CONFIG_ERRORS", errors), \
             patch.object(cx, "ensure_proxy") as ensure_proxy, \
             patch("builtins.print") as output:
            self.assertEqual(cx.main(), 1)

        ensure_proxy.assert_not_called()
        output.assert_any_call("Configuration problems in .env:")


class PromptBoundarySourceTests(unittest.TestCase):
    def test_only_the_prompt_wrapper_calls_the_raw_input(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "cx.py").read_text(encoding="utf-8")
        callers = {
            function.name
            for function in ast.walk(ast.parse(source))
            if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "input"
                for node in ast.walk(function)
            )
        }
        self.assertEqual(callers, {"_ask"})


class WindowsWrapperSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(__file__).resolve().parents[1]
        self.source = (root / "cx.bat").read_text(encoding="utf-8")

    def test_batch_forwards_original_quoted_arguments_without_reassembly(self):
        self.assertIn('uv run --project "%~dp0" python "%~dp0cx.py" %*', self.source.splitlines())
        for parser_shape in (":parse", "%~1", "CX_ARGS", "shift", "~0,-1"):
            self.assertNotIn(parser_shape, self.source)


@unittest.skipUnless(os.name == "posix", "POSIX shell wrapper test")
class PosixWrapperTests(unittest.TestCase):
    def test_shell_wrapper_preserves_empty_and_metacharacter_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = root / "arguments"
            fake_uv = root / "uv"
            fake_uv.write_text(
                "#!/bin/sh\n"
                "while [ \"$1\" != python ]; do shift; done\n"
                "shift 2\n"
                "printf '%s\\0' \"$@\" > \"$CX_CAPTURE\"\n",
                encoding="utf-8",
            )
            fake_uv.chmod(0o755)
            project = Path(__file__).resolve().parents[1]
            environment = os.environ.copy()
            environment.update(CX_CAPTURE=str(arguments), PATH=f"{root}:{environment['PATH']}")
            result = subprocess.run(
                ["bash", str(project / "cx.sh"), "", "--flag=a&b", "semi;colon"],
                cwd=root,
                env=environment,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(arguments.exists(), (result.stdout, result.stderr))
            self.assertEqual(arguments.read_bytes(), b"\0--flag=a&b\0semi;colon\0")


class RouterConsoleTests(unittest.TestCase):
    def _run_console(self, outcome, answers=("k", "")):
        replies = iter(answers)
        printed = []
        with (
            patch("cx.clear_console"),
            patch("cx.read_router_pid", return_value=4242),
            patch("cx.router_is_ready", return_value=True),
            patch("cx.stop_router", return_value=outcome),
            patch("cx._ask", side_effect=lambda _prompt: next(replies, "")),
            patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(str(x) for x in a))),
        ):
            cx._router_console()
        return "\n".join(printed)

    def test_a_refused_stop_is_never_reported_as_stopped(self):
        printed = self._run_console(StopOutcome.STOPPED)
        self.assertIn("Router stopped.", printed)
        for outcome in (StopOutcome.ABSENT, StopOutcome.REFUSED):
            with self.subTest(outcome=outcome):
                printed = self._run_console(outcome)
                self.assertNotIn("Router stopped.", printed)
                if outcome is StopOutcome.ABSENT:
                    self.assertIn("Nothing was running.", printed)
                else:
                    self.assertIn("did not stop", printed)

    def test_a_refused_stop_does_not_claim_a_restart(self):
        printed = self._run_console(StopOutcome.REFUSED, answers=("r",))
        self.assertIn("did not stop", printed)
        self.assertIn("not restarting", printed)
        self.assertNotIn("restarting…", printed)


if __name__ == "__main__":
    unittest.main()
