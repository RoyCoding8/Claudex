from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modules.router_state import RouterState, StateUnavailableError


class _Clock:
    def __init__(self, *, wall: float = 1_800_000_000, monotonic: float = 1000) -> None:
        self.wall = wall
        self.monotonic = monotonic

    def wall_time(self) -> float:
        return self.wall

    def monotonic_time(self) -> float:
        return self.monotonic

    def advance(self, seconds: float) -> None:
        self.wall += seconds
        self.monotonic += seconds


class RouterStateTests(unittest.TestCase):
    def test_live_hits_and_cooldowns_survive_router_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "router-state.json"
            first_clock = _Clock()
            first = RouterState(path, wall_time=first_clock.wall_time,
                                monotonic_time=first_clock.monotonic_time)
            self.addCleanup(first.close)
            self.assertTrue(first.reserve("provider/model", 1))
            first.cooldown("provider/model", 50)
            first.close()

            second_clock = _Clock(wall=first_clock.wall + 20, monotonic=5)
            second = RouterState(path, wall_time=second_clock.wall_time,
                                 monotonic_time=second_clock.monotonic_time)
            self.addCleanup(second.close)
            self.assertFalse(second.reserve("provider/model", 1))
            self.assertFalse(second.is_ready("provider/model"))
            second_clock.advance(41)
            self.assertTrue(second.is_ready("provider/model"))
            self.assertTrue(second.reserve("provider/model", 1))
            second.close()

    def test_expired_state_is_not_restored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "router-state.json"
            clock = _Clock()
            state = RouterState(path, wall_time=clock.wall_time,
                                monotonic_time=clock.monotonic_time)
            self.addCleanup(state.close)
            self.assertTrue(state.reserve("provider/model", 1))
            state.cooldown("provider/model", 30)
            state.close()

            restarted_clock = _Clock(wall=clock.wall + 61, monotonic=1)
            restarted = RouterState(path, wall_time=restarted_clock.wall_time,
                                     monotonic_time=restarted_clock.monotonic_time)
            self.addCleanup(restarted.close)
            self.assertTrue(restarted.is_ready("provider/model"))
            self.assertTrue(restarted.reserve("provider/model", 1))
            restarted.close()

    def test_clear_removes_cooldown_but_keeps_rate_limit_hits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = RouterState(Path(temporary) / "router-state.json")
            self.addCleanup(state.close)
            self.assertTrue(state.reserve("provider/model", 1))
            state.cooldown("provider/model", 30)
            state.clear("provider/model")
            self.assertTrue(state.is_ready("provider/model"))
            self.assertFalse(state.reserve("provider/model", 1))
            state.close()

    def test_corrupt_state_fails_closed_without_replacing_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "router-state.json"
            corrupt = b'{"version":1,"version":1,"models":{}}'
            path.write_bytes(corrupt)
            with self.assertRaises(StateUnavailableError):
                RouterState(path)
            self.assertEqual(path.read_bytes(), corrupt)

    def test_reservations_use_memory_without_writing_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = RouterState(Path(temporary) / "router-state.json")
            self.addCleanup(state.close)
            with patch("modules.router_state._publish_state") as publish:
                self.assertTrue(state.reserve("provider/model", 1))
                self.assertFalse(state.reserve("provider/model", 1))
            publish.assert_not_called()
            state.close()

    def test_failed_cooldown_write_still_protects_the_live_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = RouterState(Path(temporary) / "router-state.json")
            try:
                with patch("modules.router_state._publish_state", side_effect=OSError("disk full")), \
                        self.assertLogs("cx.router.state", level="ERROR"):
                    state.cooldown("provider/model", 30)
                self.assertFalse(state.is_ready("provider/model"))
            finally:
                state.close()

    def test_only_one_process_can_own_a_state_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "router-state.json"
            first = RouterState(path)
            self.addCleanup(first.close)
            with self.assertRaises(StateUnavailableError):
                RouterState(path)
            first.close()
            second = RouterState(path)
            self.addCleanup(second.close)
            second.close()


if __name__ == "__main__":
    unittest.main()
