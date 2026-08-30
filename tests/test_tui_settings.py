"""Settings-file corruption recovery (H14) and lost-update guard (H8)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class SettingsTests(unittest.TestCase):
    def test_non_utf8_settings_is_recovered(self):
        from modules import tui
        with tempfile.TemporaryDirectory() as directory:
            data_dir, settings = Path(directory) / "data", Path(directory) / "data" / "settings.json"
            data_dir.mkdir()
            settings.write_bytes(b'\xff\xfe{"model_settings":{}}')
            with patch.object(tui, "SETTINGS_FILE", settings), patch.object(tui, "DATA_DIR", data_dir):
                loaded = tui._load_settings()
            backups = list(data_dir.glob("settings.broken_*.json"))
        self.assertEqual(loaded, {})
        self.assertEqual(len(backups), 1)

    def test_save_settings_detects_concurrent_modification(self):
        from modules import tui
        with tempfile.TemporaryDirectory() as directory:
            data_dir, settings = Path(directory) / "data", Path(directory) / "data" / "settings.json"
            data_dir.mkdir()
            settings.write_text('{"category": "All"}', encoding="utf-8")
            with patch.object(tui, "SETTINGS_FILE", settings), patch.object(tui, "DATA_DIR", data_dir):
                tui._load_settings()
                settings.write_text('{"category": "Grok"}', encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "changed on disk"):
                    tui._save_settings({"category": "Codex"})
                self.assertEqual(json.loads(settings.read_text(encoding="utf-8")), {"category": "Grok"})


if __name__ == "__main__":
    unittest.main()
