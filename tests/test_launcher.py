from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modules import launcher
from modules.launcher import _clean_extra_args, launch_claude


class LauncherTests(unittest.TestCase):
    def test_removes_conflicting_model_and_permission_args(self) -> None:
        self.assertEqual(
            _clean_extra_args(
                ["--model", "old", "--model=also-old", "--dangerously-skip-permissions", "--verbose"]
            ),
            ["--verbose"],
        )

    @patch("modules.launcher.subprocess.call", return_value=0)
    @patch("modules.launcher.shutil.which", return_value="/usr/bin/claude")
    @patch.object(launcher, "ROUTER_PORT", 47111)
    def test_claude_points_to_router(self, _which, call) -> None:
        result = launch_claude(
            "glm-pool",
            False,
            100000,
            True,
            [],
        )
        self.assertEqual(result, 0)
        kwargs = call.call_args.kwargs
        env = kwargs["env"]
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "http://127.0.0.1:47111")
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "sk-cx-local")
        self.assertEqual(env["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"], "1")
        self.assertEqual(env["ANTHROPIC_MODEL"], "glm-pool")
        self.assertEqual(env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"], "100000")
        self.assertEqual(env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "85000")

    @patch.dict(
        os.environ,
        {
            "PATH": "/usr/bin",
            "HOME": "/home/tester",
            "HTTPS_PROXY": "http://proxy.test:8443",
            "CX_CLIPROXY_API_KEY": "proxy-secret",
            "CX_CLIPROXY_CONFIG": "/private/proxy.yaml",
            "CX_ROUTER_HOST": "router.test",
            "CX_ROUTER_API_KEY": "router-secret",
            "CX_ROUTER_COOLDOWN_429": "90",
            # os.environ is case-insensitive on win32, so both spellings collapse
            # to one key here; a case-sensitive mapping carries them, below.
            "NO_PROXY": "example.com,internal.test",
        },
        clear=True,
    )
    @patch("modules.launcher.subprocess.call", return_value=0)
    @patch("modules.launcher.shutil.which", return_value="claude")
    @patch.object(launcher, "ROUTER_PORT", 47111)
    def test_child_environment_isolates_cx_config_and_bypasses_local_router(self, _which, call) -> None:
        launch_claude("glm-pool", False, None, None, [])

        environment = call.call_args.kwargs["env"]
        self.assertTrue(all(not name.upper().startswith("CX_") for name in environment))
        self.assertEqual(environment["PATH"], "/usr/bin")
        self.assertEqual(environment["HOME"], "/home/tester")
        self.assertEqual(environment["HTTPS_PROXY"], "http://proxy.test:8443")
        self.assertEqual(environment["ANTHROPIC_BASE_URL"], "http://127.0.0.1:47111")
        self.assertEqual(environment["ANTHROPIC_AUTH_TOKEN"], "sk-cx-local")
        self.assertEqual(environment["NO_PROXY"], "example.com,internal.test,127.0.0.1")
        self.assertEqual(environment["no_proxy"], "example.com,internal.test,127.0.0.1")

    @patch.dict(
        os.environ,
        {"NO_PROXY": "example.com,internal.test", "HTTPS_PROXY": "http://proxy.test:8443"},
        clear=True,
    )
    @patch("modules.launcher.subprocess.call", return_value=0)
    @patch("modules.launcher.shutil.which", return_value="claude")
    def test_router_host_is_always_bypassed_so_the_token_never_reaches_the_proxy(self, _which, call) -> None:
        for host in ("127.0.0.1", "192.0.2.1", "router.example.com", "::ffff:127.0.0.1"):
            with self.subTest(host=host), patch.object(launcher, "ROUTER_HOST", host):
                launch_claude("glm-pool", False, None, None, [])

                environment = call.call_args.kwargs["env"]
                self.assertEqual(environment["NO_PROXY"], f"example.com,internal.test,{host}")
                self.assertEqual(environment["no_proxy"], f"example.com,internal.test,{host}")
                self.assertEqual(environment["ANTHROPIC_AUTH_TOKEN"], "sk-cx-local")
                self.assertEqual(environment["HTTPS_PROXY"], "http://proxy.test:8443")

    def test_both_no_proxy_spellings_are_merged(self) -> None:
        # Only a case-sensitive mapping carries both spellings on win32, so this
        # covers the merge a real win32 env cannot exercise.
        cases = ({"NO_PROXY": "example.com", "no_proxy": "internal.test"},
                 {"no_proxy": "internal.test", "NO_PROXY": "example.com"})
        for inherited in cases:
            with self.subTest(inherited=sorted(inherited)), \
                 patch("modules.launcher.os.environ.copy", return_value=dict(inherited)):
                self.assertEqual(
                    launcher._no_proxy_value(dict(inherited), "127.0.0.1"),
                    "example.com,internal.test,127.0.0.1",
                )

    def test_a_host_already_in_no_proxy_is_not_duplicated(self) -> None:
        self.assertEqual(
            launcher._no_proxy_value({"NO_PROXY": "127.0.0.1,example.com"}, "127.0.0.1"),
            "127.0.0.1,example.com",
        )

    @patch("modules.launcher.subprocess.call", return_value=0)
    @patch("modules.launcher.shutil.which", return_value="claude")
    def test_claude_formats_ipv6_router_url_without_changing_socket_config(self, _which, call) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(launcher, "ROUTER_HOST", "::1"),
            patch.object(launcher, "ROUTER_PORT", 4000),
        ):
            result = launch_claude("glm-pool", False, None, None, [])

        self.assertEqual(result, 0)
        environment = call.call_args.kwargs["env"]
        self.assertEqual(environment["ANTHROPIC_BASE_URL"], "http://[::1]:4000")
        self.assertEqual(environment["NO_PROXY"], "::1")
        self.assertEqual(environment["no_proxy"], "::1")

    @unittest.skipUnless(os.name == "posix", "POSIX process argument gate")
    def test_fake_claude_receives_handoff_arguments_and_its_exit_code_unchanged(self) -> None:
        cases = (
            ("claude.cmd", '"$0" "$@"', 23, True),
            ("claude", '"$@"', 0, False),
        )
        for name, printf, exit_code, prints_argv0 in cases:
            with self.subTest(claude=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                arguments = root / "arguments"
                fake_claude = root / name
                fake_claude.write_text(
                    f"#!/bin/sh\nprintf '%s\\0' {printf} > \"$TEST_CAPTURE\"\nexit {exit_code}\n",
                    encoding="utf-8",
                )
                fake_claude.chmod(0o755)
                with (
                    patch.dict(os.environ, {"TEST_CAPTURE": str(arguments)}),
                    patch.object(launcher.shutil, "which", return_value=str(fake_claude)),
                ):
                    result = launch_claude(
                        "model&calc.exe",
                        False,
                        None,
                        None,
                        ["", "input=a|b", "semi;colon"],
                    )

                self.assertEqual(result, exit_code)
                handoff = b"--model\0model&calc.exe\0\0input=a|b\0semi;colon\0"
                prefix = str(fake_claude).encode() + b"\0" if prints_argv0 else b""
                self.assertEqual(arguments.read_bytes(), prefix + handoff)

    def test_wrapper_dispatch_preserves_argument_vectors(self) -> None:
        cases = (
            (
                ".cmd",
                r"C:\tools\claude.cmd",
                [r"C:\tools\claude.cmd", "--model", "model&calc.exe", ""],
            ),
            (
                ".ps1",
                r"C:\tools\claude.ps1",
                [
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    r"C:\tools\claude.ps1",
                    "--model",
                    "model&calc.exe",
                    "",
                ],
            ),
        )
        for suffix, path, expected in cases:
            with self.subTest(suffix=suffix):
                with (
                    patch.object(launcher.shutil, "which", return_value=path),
                    patch("modules.launcher.subprocess.call", return_value=0) as call,
                ):
                    result = launch_claude("model&calc.exe", False, None, None, [""])

                self.assertEqual(result, 0)
                self.assertEqual(call.call_args.args[0], expected)

    @patch("modules.launcher.shutil.which", return_value=None)
    def test_missing_claude_raises_runtime_error(self, _which) -> None:
        with self.assertRaisesRegex(RuntimeError, "not found"):
            launch_claude("m", False, None, None, [])

    @patch("modules.launcher.subprocess.call", side_effect=OSError(193, "not a valid Win32 application"))
    @patch("modules.launcher.shutil.which", return_value="C:\\tools\\claude.ps1")
    def test_subprocess_oserror_becomes_runtime_error(self, _which, _call) -> None:
        with self.assertRaisesRegex(RuntimeError, "Failed to launch"):
            launch_claude("m", False, None, None, [])

    @patch("modules.launcher.subprocess.call", return_value=0)
    @patch("modules.launcher.shutil.which", return_value="claude")
    def test_inherited_launch_environment_is_cleared_of_compaction_and_provider_choices(
        self, _which, call
    ) -> None:
        for group, variables in (
            ("compaction", (
                "CLAUDE_CODE_MAX_CONTEXT_TOKENS",
                "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
                "DISABLE_AUTO_COMPACT",
                "DISABLE_COMPACT",
            )),
            ("provider selectors", (
                "CLAUDE_CODE_USE_ANTHROPIC_AWS",
                "CLAUDE_CODE_USE_BEDROCK",
                "CLAUDE_CODE_USE_FOUNDRY",
                "CLAUDE_CODE_USE_MANTLE",
                "CLAUDE_CODE_USE_VERTEX",
            )),
        ):
            with self.subTest(group=group), patch.dict(
                os.environ, {variable: "1" for variable in variables}
            ):
                launch_claude("m", False, None, None, [])
                environment = call.call_args.kwargs["env"]
                for variable in variables:
                    self.assertNotIn(variable, environment)

    @patch("modules.launcher.subprocess.call", return_value=0)
    @patch("modules.launcher.shutil.which", return_value="claude")
    def test_openai_family_false_ignores_gpt_defaults(self, _which, call) -> None:
        with patch.object(launcher, "DEFAULT_GPT_FAST_MODEL", "openai/gpt-default"):
            launch_claude("gpt-fast-pool", False, None, None, [], openai_family=False)
        env = call.call_args.kwargs["env"]
        self.assertEqual(env["ANTHROPIC_SMALL_FAST_MODEL"], "gpt-fast-pool")


if __name__ == "__main__":
    unittest.main()
