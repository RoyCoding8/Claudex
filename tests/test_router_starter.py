from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from modules import router_starter as rs


class PidIdentityTests(unittest.TestCase):
    def test_unverifiable_pid_is_never_killed(self):
        with patch.object(rs, "_pid_is_alive", return_value=False):
            self.assertFalse(rs._pid_is_router(123))


class StartupLockTests(unittest.TestCase):
    def test_second_launcher_waits_instead_of_spawning(self):
        with patch("modules.router_starter.subprocess.Popen") as popen, \
             patch.object(rs, "router_is_ready", side_effect=[False, True]), \
             patch.object(rs, "_port_is_open", return_value=False), \
             patch.object(rs, "_startup_lock", return_value=None):
            rs.ensure_router()
        popen.assert_not_called()

    def test_lock_holder_spawns_exactly_once(self):
        with patch("modules.router_starter.subprocess.Popen", return_value=MagicMock(poll=lambda: None)) as popen, \
             patch.object(rs, "router_is_ready", side_effect=[False, False, True]), \
             patch.object(rs, "_port_is_open", return_value=False), \
             patch.object(rs, "_startup_lock", return_value=MagicMock()), \
             patch.object(rs, "_rotate_spawn_log"), \
             patch.object(rs, "ROUTER_PID"), \
             patch.object(rs, "ROUTER_LOG"):
            rs.ensure_router()
        popen.assert_called_once()

    def test_stale_lock_is_reclaimed(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "router.lock"
            lock_path.write_text("999", encoding="ascii")
            stale = time.time() - 120
            os.utime(lock_path, (stale, stale))
            with patch.object(rs, "ROUTER_LOG", Path(directory) / "router.log"):
                handle = rs._startup_lock()
            self.assertIsNotNone(handle)
            handle.close()


if __name__ == "__main__":
    unittest.main()
