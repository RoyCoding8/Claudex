from __future__ import annotations

import os
import unittest
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
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "http://127.0.0.1:4000")
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "sk-cx-local")
        self.assertEqual(env["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"], "1")
        self.assertEqual(env["ANTHROPIC_MODEL"], "glm-pool")


    @patch("modules.launcher.subprocess.call", return_value=0)
    @patch("modules.launcher.shutil.which", return_value="C:\\tools\\claude.cmd")
    def test_cmd_dispatch_routes_through_cmd(self, _which, call) -> None:
        launch_claude("m", False, None, None, [])
        argv = call.call_args.args[0]
        self.assertEqual(argv[:3], [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c"][:3])

    @patch("modules.launcher.subprocess.call", return_value=0)
    @patch("modules.launcher.shutil.which", return_value="C:\\tools\\claude.ps1")
    def test_ps1_dispatch_routes_through_powershell(self, _which, call) -> None:
        launch_claude("m", False, None, None, [])
        argv = call.call_args.args[0]
        self.assertEqual(argv[0], "powershell")
        self.assertIn("-File", argv)
        self.assertIn("--model", argv)

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
    def test_auto_compact_default_leaves_window_unset(self, _which, call) -> None:
        launch_claude("m", False, None, None, [])
        env = call.call_args.kwargs["env"]
        self.assertNotIn("CLAUDE_CODE_AUTO_COMPACT_WINDOW", env)
        self.assertNotIn("DISABLE_AUTO_COMPACT", env)

    @patch("modules.launcher.subprocess.call", return_value=0)
    @patch("modules.launcher.shutil.which", return_value="claude")
    def test_openai_family_false_ignores_gpt_defaults(self, _which, call) -> None:
        with patch.object(launcher, "DEFAULT_GPT_FAST_MODEL", "openai/gpt-default"):
            launch_claude("gpt-fast-pool", False, None, None, [], openai_family=False)
        env = call.call_args.kwargs["env"]
        self.assertEqual(env["ANTHROPIC_SMALL_FAST_MODEL"], "gpt-fast-pool")


if __name__ == "__main__":
    unittest.main()
