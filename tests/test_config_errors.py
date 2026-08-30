"""Config problems must be collected and reported, never raised at import (H9, M37)."""
from __future__ import annotations

import importlib
import os
import unittest

_SNAPSHOT_KEYS = ("ROUTER_PORT", "ROUTER_POOL_PASSES", "ROUTER_COOLDOWN_429", "CONFIG_ERRORS")


def _reload_with_env(**values) -> dict:
    """Reload modules.config with extra env; snapshot and restore around the reload."""
    import modules.config as config
    saved = {name: os.environ.get(name) for name in values}
    try:
        os.environ.update({name: value for name, value in values.items() if value is not None})
        importlib.reload(config)
        return {key: getattr(config, key) for key in _SNAPSHOT_KEYS}
    finally:
        for name, old in saved.items():
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old
        importlib.reload(config)


class ConfigErrorTests(unittest.TestCase):
    def test_negative_port_is_reported(self):
        snapshot = _reload_with_env(CX_ROUTER_PORT="-5")
        self.assertTrue(any("CX_ROUTER_PORT" in e for e in snapshot["CONFIG_ERRORS"]))
        self.assertEqual(snapshot["ROUTER_PORT"], 4000)

    def test_out_of_bounds_values_are_collected(self):
        snapshot = _reload_with_env(CX_ROUTER_POOL_PASSES="999", CX_ROUTER_COOLDOWN_429="99999")
        self.assertEqual(len(snapshot["CONFIG_ERRORS"]), 2)
        self.assertEqual(snapshot["ROUTER_POOL_PASSES"], 2)
        self.assertEqual(snapshot["ROUTER_COOLDOWN_429"], 60.0)

    def test_valid_env_still_applies(self):
        snapshot = _reload_with_env(CX_ROUTER_PORT="5050")
        self.assertEqual(snapshot["ROUTER_PORT"], 5050)
        self.assertEqual(snapshot["CONFIG_ERRORS"], [])


if __name__ == "__main__":
    unittest.main()
