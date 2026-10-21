from __future__ import annotations

import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from modules import launcher
from modules.launcher import _clean_extra_args, launch_client


class LauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        client = patch.object(launcher, "ACTIVE_CLIENT", "claude")
        client.start()
        self.addCleanup(client.stop)

    @patch("modules.launcher.subprocess.call", return_value=0)
    @patch("modules.launcher.shutil.which", return_value="codex")
    @patch.object(launcher, "ROUTER_API_KEY", new="router-secret-unique")
    @patch.object(launcher, "ROUTER_HOST", new="router.local")
    @patch.object(launcher, "ROUTER_PORT", new=47111)
    def test_codex_client_uses_invocation_scoped_router_and_role_config(self, _which, call) -> None:
        observed: dict = {}
        explorer_model = 'provider/explorer "quoted" \\ snow/🦦'

        def inspect_call(command, *, env):
            observed["command"] = command
            observed["env"] = env
            overrides = [command[index + 1] for index, value in enumerate(command[:-1]) if value == "-c"]
            observed["config"] = tomllib.loads("\n".join(overrides))
            agents = observed["config"]["agents"]
            observed["role_paths"] = []
            observed["role_toml"] = {}
            for name in ("explorer", "worker"):
                path = Path(agents[name]["config_file"])
                self.assertTrue(path.is_file())
                observed["role_paths"].append(path)
                observed["role_toml"][name] = tomllib.loads(path.read_text(encoding="utf-8"))
            return 0

        with (
            patch.object(launcher, "ACTIVE_CLIENT", "codex"),
            patch("modules.launcher.subprocess.call", side_effect=inspect_call),
            patch.dict(
                os.environ,
                {
                    "PATH": "codex-path",
                    "CODEX_HOME": "user-codex-home",
                    "CX_ROUTER_API_KEY": "stale-key",
                    "CX_OTHER_SETTING": "must-not-leak",
                    "HTTPS_PROXY": "http://proxy.test:8443",
                    "NO_PROXY": "example.com",
                },
                clear=True,
            ),
        ):
            result = launcher.launch_client(
                "root/model with spaces", True, 200000, True,
                ["-m", "wrong", "--model=wrong-too", "--yolo", "--ask-for-approval", "never",
                 "--sandbox=read-only", "--", "--model", "prompt text", "--dangerously-bypass-approvals-and-sandbox"],
                role_models={
                    "codex_default_subagent_model": "provider/sub agent",
                    "codex_explorer_model": explorer_model,
                },
            )

        self.assertEqual(result, 0)
        command = observed["command"]
        environment = observed["env"]
        config = observed["config"]
        self.assertEqual(command[1:3], ["--model", "root/model with spaces"])
        separator = command.index("--")
        self.assertNotIn("-m", command[:separator])
        self.assertNotIn("--yolo", command[:separator])
        self.assertNotIn("--ask-for-approval", command[:separator])
        self.assertNotIn("--sandbox=read-only", command[:separator])
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", command[:separator])
        self.assertEqual(
            command[separator:],
            ["--", "--model", "prompt text", "--dangerously-bypass-approvals-and-sandbox"],
        )
        self.assertEqual(config["model_provider"], "cx")
        self.assertEqual(config["model_providers"]["cx"]["base_url"], "http://router.local:47111/v1")
        self.assertEqual(config["model_providers"]["cx"]["wire_api"], "responses")
        self.assertFalse(config["model_providers"]["cx"]["requires_openai_auth"])
        self.assertFalse(config["model_providers"]["cx"]["supports_websockets"])
        self.assertEqual(config["model_providers"]["cx"]["capabilities"]["remote_compaction"], "unsupported")
        self.assertEqual(config["model_context_window"], 200000)
        self.assertEqual(config["model_auto_compact_token_limit"], 170000)
        self.assertEqual(config["agents"]["default_subagent_model"], "provider/sub agent")
        self.assertEqual(config["agents"]["explorer"]["description"], "Explore the codebase and report findings without changing files.")
        self.assertEqual(observed["role_toml"]["explorer"]["model"], explorer_model)
        self.assertEqual(observed["role_toml"]["worker"]["model"], "root/model with spaces")
        self.assertTrue(all(not path.exists() for path in observed["role_paths"]))
        self.assertEqual(environment["CX_ROUTER_API_KEY"], "router-secret-unique")
        self.assertNotIn("CX_OTHER_SETTING", environment)
        self.assertEqual(environment["CODEX_HOME"], "user-codex-home")
        self.assertNotIn("router-secret-unique", command)
        self.assertEqual(environment["NO_PROXY"], "example.com,router.local")
        self.assertEqual(environment["no_proxy"], "example.com,router.local")

    @patch("modules.launcher.subprocess.call", return_value=0)
    @patch("modules.launcher.shutil.which", return_value="codex")
    def test_codex_stored_auto_compact_off_does_not_fake_a_disable(self, _which, call) -> None:
        with patch.object(launcher, "ACTIVE_CLIENT", "codex"):
            launcher.launch_client("root/model", False, 128000, False, [])

        command = call.call_args.args[0]
        overrides = [command[index + 1] for index, value in enumerate(command[:-1]) if value == "-c"]
        config = tomllib.loads("\n".join(overrides))
        self.assertEqual(config["model_context_window"], 128000)
        self.assertNotIn("model_auto_compact_token_limit", config)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)

    @patch("modules.launcher.subprocess.call", return_value=0)
    @patch("modules.launcher.shutil.which", return_value="codex")
    def test_codex_auto_compact_without_context_uses_the_default_window(self, _which, call) -> None:
        with patch.object(launcher, "ACTIVE_CLIENT", "codex"), patch.object(
            launcher, "DEFAULT_COMPACT_WINDOW", 123456
        ):
            launcher.launch_client("root/model", False, None, True, [])

        command = call.call_args.args[0]
        overrides = [command[index + 1] for index, value in enumerate(command[:-1]) if value == "-c"]
        config = tomllib.loads("\n".join(overrides))
        self.assertNotIn("model_context_window", config)
        self.assertEqual(config["model_auto_compact_token_limit"], 123456)

    @patch("modules.launcher.subprocess.call", return_value=0)
    @patch("modules.launcher.shutil.which", return_value="C:\\tools\\codex.ps1")
    def test_codex_uses_the_existing_powershell_wrapper(self, _which, call) -> None:
        with patch.object(launcher, "ACTIVE_CLIENT", "codex"):
            launcher.launch_client("root/model", False, None, None, [])

        command = call.call_args.args[0]
        self.assertEqual(
            command[:6],
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "C:\\tools\\codex.ps1"],
        )
        self.assertEqual(command[6:8], ["--model", "root/model"])

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
        result = launch_client(
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
        launch_client("glm-pool", False, None, None, [])

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
                launch_client("glm-pool", False, None, None, [])

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
            result = launch_client("glm-pool", False, None, None, [])

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
                    result = launch_client(
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
                    result = launch_client("model&calc.exe", False, None, None, [""])

                self.assertEqual(result, 0)
                self.assertEqual(call.call_args.args[0], expected)

    @patch("modules.launcher.shutil.which", return_value=None)
    def test_missing_selected_client_raises_runtime_error(self, _which) -> None:
        with self.assertRaisesRegex(RuntimeError, "not found"):
            launch_client("m", False, None, None, [])

    @patch("modules.launcher.subprocess.call", side_effect=OSError(193, "not a valid Win32 application"))
    @patch("modules.launcher.shutil.which", return_value="C:\\tools\\claude.ps1")
    def test_subprocess_oserror_becomes_runtime_error(self, _which, _call) -> None:
        with self.assertRaisesRegex(RuntimeError, "Failed to launch"):
            launch_client("m", False, None, None, [])

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
                launch_client("m", False, None, None, [])
                environment = call.call_args.kwargs["env"]
                for variable in variables:
                    self.assertNotIn(variable, environment)

    @patch("modules.launcher.subprocess.call", return_value=0)
    @patch("modules.launcher.shutil.which", return_value="claude")
    def test_openai_family_false_ignores_gpt_defaults(self, _which, call) -> None:
        with patch.dict(
            launcher.CLIENTS,
            {"claude": ("Claude Code", (("gpt_fast_model", "Fast/Haiku", ("env", "ANTHROPIC_SMALL_FAST_MODEL"), "openai/gpt-default", ""),))},
        ):
            launch_client("gpt-fast-pool", False, None, None, [], openai_family=False)
        env = call.call_args.kwargs["env"]
        self.assertEqual(env["ANTHROPIC_SMALL_FAST_MODEL"], "gpt-fast-pool")

    @patch("modules.launcher.subprocess.call", return_value=0)
    @patch("modules.launcher.shutil.which", return_value="claude")
    def test_role_env_vars_carry_chosen_models(self, _which, call) -> None:
        launch_client(
            "glm-pool", False, None, None, [],
            role_models={
                "gpt_fast_model": "x/fast", "gpt_medium_model": "x/medium",
                "gpt_subagent_model": "x/subagent", "gpt_fable_model": "x/fable",
            },
        )
        env = call.call_args.kwargs["env"]
        self.assertEqual(env["ANTHROPIC_DEFAULT_HAIKU_MODEL"], "x/fast")
        self.assertEqual(env["ANTHROPIC_DEFAULT_SONNET_MODEL"], "x/medium")
        self.assertEqual(env["ANTHROPIC_DEFAULT_OPUS_MODEL"], "glm-pool")
        self.assertEqual(env["ANTHROPIC_DEFAULT_FABLE_MODEL"], "x/fable")
        self.assertEqual(env["CLAUDE_CODE_SUBAGENT_MODEL"], "x/subagent")

    @patch("modules.launcher.subprocess.call", return_value=0)
    @patch("modules.launcher.shutil.which", return_value="claude")
    def test_fable_role_defaults_to_the_launch_model(self, _which, call) -> None:
        launch_client("glm-pool", False, None, None, [])
        env = call.call_args.kwargs["env"]
        self.assertEqual(env["ANTHROPIC_DEFAULT_FABLE_MODEL"], "glm-pool")


if __name__ == "__main__":
    unittest.main()
