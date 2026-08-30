from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modules.pool_tui import _edit_members_flow
from modules.pools import _LOADED_DIGESTS, ModelPool, PoolMember, load_pools, save_pools


class PoolHardeningTests(unittest.TestCase):
    def tearDown(self) -> None:
        _LOADED_DIGESTS.clear()

    def test_priority_zero_and_single_member_pool_load(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pools.json"
            path.write_text(json.dumps({"pools": [{"name": "one", "members": [{"model": "a/x", "priority": 0}]}]}), encoding="utf-8")
            pools = load_pools(path)
        self.assertEqual(pools[0].members[0].priority, 0)
        self.assertEqual(len(pools[0].members), 1)

    def test_priority_zero_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pools.json"
            save_pools([ModelPool("p", (PoolMember("a/x", priority=0),))], path)
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["pools"][0]["members"][0]["priority"], 0)

    def test_save_refuses_lost_update(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pools.json"
            path.write_text('{"pools": []}', encoding="utf-8")
            load_pools(path)
            path.write_text('{"pools": [{"name":"external","members":[]}]}' , encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "changed on disk"):
                save_pools([], path)

    def test_editing_a_member_preserves_limit_and_cooldown(self) -> None:
        members = [PoolMember("a/x", rpm=10, priority=0, limit=30, cooldown=15.0), PoolMember("b/x")]
        actions = iter([type("Action", (), {"kind": "edit", "index": 0})(), type("Action", (), {"kind": "cancel", "index": -1})()])
        with (
            patch("modules.pool_tui._member_editor_tui", side_effect=lambda *_: next(actions)),
            patch("modules.pool_tui._clear"),
            patch("modules.pool_tui._prompt_int", side_effect=[11, 0]),
        ):
            _edit_members_flow("p", members, [])
        self.assertEqual(members[0].limit, 30)
        self.assertEqual(members[0].cooldown, 15.0)
        self.assertEqual(members[0].priority, 0)


if __name__ == "__main__":
    unittest.main()
