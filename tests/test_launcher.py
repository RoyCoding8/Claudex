from __future__ import annotations

import unittest
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
