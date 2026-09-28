from __future__ import annotations

import asyncio
import hashlib
import io
import json
import multiprocessing
import os
import tempfile
import threading
import tracemalloc
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, call, patch

from prompt_toolkit.application import Application
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output.plain_text import PlainTextOutput

from modules import tui
from modules.models import Model
from modules.tui import (
    PickerResult,
    configure_model_parameters,
    get_model_autocompact,
    get_model_context,
    set_extra_model,
)


class _FixedScreenOutput(PlainTextOutput):
    def __init__(self, columns: int, rows: int) -> None:
        super().__init__(io.StringIO())
        self.columns = columns
        self.rows = rows

    def get_size(self) -> Size:
        return Size(rows=self.rows, columns=self.columns)


class _PickerSession:
    def __init__(self, keys=(), **keywords) -> None:
        self.key_bindings = keywords["key_bindings"]
        self.layout = keywords["layout"]
        self.style = keywords["style"]
        self.keys = [[str(key)] for key in keys]
        self.screens: list[str] = []
        self.result: PickerResult | None = None
        self.is_done = False

    def invalidate(self) -> None:
        pass

    def exit(self, result) -> None:
        self.result = result
        self.is_done = True

    async def render(self, application: Application, columns: int) -> str:
        application.render_counter += 1
        application.renderer.render(application, application.layout)
        screen = application.renderer.last_rendered_screen
        assert screen is not None
        return "\n".join(
            "".join(screen.data_buffer[row][column].char for column in range(columns)).rstrip()
            for row in range(max(sorted(screen.data_buffer), default=-1) + 1)
        )

    async def session(self, columns: int = 81, rows: int = 40) -> None:
        with create_pipe_input() as pipe_input:
            application: Application[PickerResult] = Application(
                layout=self.layout,
                key_bindings=self.key_bindings,
                style=self.style,
                full_screen=True,
                input=pipe_input,
                output=_FixedScreenOutput(columns, rows),
            )
            with set_app(application):
                self.screens.append(await self.render(application, columns))
                for keys in self.keys:
                    if not keys[0].startswith("Keys."):
                        application.layout.current_control.buffer.text += keys[0]
                    else:
                        binding = next(
                            binding
                            for binding in self.key_bindings.bindings
                            if [str(key) for key in binding.keys] == keys
                        )
                        binding.handler(type("Event", (), {"app": self})())
                    self.screens.append(await self.render(application, columns))

    def run(self, columns: int = 81, rows: int = 40):
        asyncio.run(self.session(columns, rows))
        return self.result


def _picker_sessions(keys=(), session_class=_PickerSession):
    built = []

    def build(**keywords):
        session = session_class(keys, **keywords)
        built.append(session)
        return session

    return build, built


def _run_picker_session(tui, data_dir, settings, example_file) -> str:
    build, sessions = _picker_sessions()
    with (
        patch.object(tui, "Application", build),
        patch.object(tui, "SETTINGS_FILE", settings),
        patch.object(tui, "DATA_DIR", data_dir),
        patch.object(tui, "SETTINGS_EXAMPLE_FILE", example_file),
        patch("modules.router_starter.router_is_ready", return_value=False),
    ):
        tui.run_picker([Model("provider/model", "provider")])

    return sessions[0].screens[-1]


_FOOTER_KEYS = {
    "↑↓": ("Keys.Down",),
    "Enter": ("Keys.ControlM",),
    "Tab": ("Keys.ControlI",),
    "Del": ("Keys.Delete",),
    "Esc": ("Keys.Escape",),
    "Ctrl+S": ("Keys.ControlS",),
    "F5": ("Keys.F5",),
    "F6": ("Keys.F6",),
    "F7": ("Keys.F7",),
    "F8": ("Keys.F8",),
    "F9": ("Keys.F9",),
    "F10": ("Keys.F10",),
}


def _drive_picker(
    keys=(),
    initial=None,
    columns=81,
    rows=40,
    models=None,
    sub_picker=False,
    during=None,
    **patches,
):

    with tempfile.TemporaryDirectory() as directory:
        data_dir = Path(directory) / "data"
        settings = data_dir / "settings.json"
        data_dir.mkdir()
        settings.write_bytes(
            initial if isinstance(initial, bytes) else json.dumps(initial or {}).encode()
        )

        class During(_PickerSession):
            def run(self, current=columns, current_rows=rows):
                if during is not None:
                    during(settings)
                return super().run(current, current_rows)

        build, sessions = _picker_sessions(keys, During)
        with ExitStack() as stack:
            stack.enter_context(patch.object(tui, "Application", build))
            stack.enter_context(patch.object(tui, "SETTINGS_FILE", settings))
            stack.enter_context(patch.object(tui, "DATA_DIR", data_dir))
            stack.enter_context(patch.object(tui, "SETTINGS_EXAMPLE_FILE", data_dir / "missing.json"))
            stack.enter_context(patch("modules.router_starter.router_is_ready", return_value=False))
            for name, value in patches.items():
                stack.enter_context(patch.object(tui, name, value))
            result = tui.run_picker(
                models or [Model("provider/model", "provider")], sub_picker=sub_picker
            )
        return result, settings.read_bytes(), sessions[0], sorted(
            path.name for path in data_dir.iterdir()
        )


def _footer_text(screen: str) -> str:
    rows = screen.splitlines()
    borders = [index for index, row in enumerate(rows) if row and set(row) == {"─"}]
    if not borders:
        return ""
    return "".join("".join(rows[borders[-1] + 1:]).split())


def _write_settings_in_process(settings_path, key, value, barrier, results) -> None:

    tui.SETTINGS_FILE = Path(settings_path)
    barrier.wait()
    try:
        set_extra_model(key, value)
    except BaseException as error:
        results.put(("error", type(error).__name__))
    else:
        results.put(("ok", key))


class SettingsTests(unittest.TestCase):
    def _assert_shows(self, screen: str, text: str) -> None:
        self.assertIn("".join(text.split()), "".join(screen.split()))

    def _assert_roles_row(self, screen: str) -> None:
        rows = [row for row in screen.splitlines() if "Fast:" in row]
        self.assertEqual(len(rows), 1, screen)
        for label in ("Fast:", "Medium:", "Subagent:", "GW:"):
            self.assertIn(label, rows[0])

    def test_malformed_nested_model_settings_are_removed_on_save(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir, settings = Path(directory) / "data", Path(directory) / "data" / "settings.json"
            data_dir.mkdir()
            settings.write_text(
                json.dumps(
                    {
                        "category": "Pools",
                        "unrelated": "keep",
                        "model_settings": {
                            "valid/model": {
                                "context_tokens": 200000,
                                "auto_compact": True,
                                "custom_parameter": "keep",
                            },
                            "malformed/model": {
                                "context_tokens": "200000",
                                "auto_compact": 1,
                                "custom_parameter": "keep",
                            },
                            "not-an-object": [],
                        }
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(tui, "SETTINGS_FILE", settings), patch.object(tui, "DATA_DIR", data_dir):
                set_extra_model("gpt_fast_model", "valid/model")

            saved = json.loads(settings.read_text(encoding="utf-8"))

        self.assertEqual(saved["category"], "Pools")
        self.assertEqual(saved["unrelated"], "keep")
        self.assertEqual(
            saved["model_settings"],
            {
                "valid/model": {
                    "context_tokens": 200000,
                    "auto_compact": True,
                    "custom_parameter": "keep",
                },
                "malformed/model": {"custom_parameter": "keep"},
            },
        )

    def test_the_model_readers_admit_only_values_of_their_own_type(self):
        cases = (
            (
                get_model_autocompact, "auto_compact",
                {"enabled": True, "disabled": False, "malformed": "yes"},
                {"enabled": True, "disabled": False, "malformed": None},
            ),
            (
                get_model_context, "context_tokens",
                {"configured": 200000, "boolean": True, "zero": 0, "text": "200000"},
                {"configured": 200000, "boolean": None, "zero": None, "text": None},
            ),
        )
        for reader, field, configured, expected in cases:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                settings = Path(directory) / "settings.json"
                settings.write_text(json.dumps({"model_settings": {
                    model: {field: value} for model, value in configured.items()
                }}), encoding="utf-8")
                with patch.object(tui, "SETTINGS_FILE", settings):
                    for model, value in expected.items():
                        if isinstance(value, bool):
                            self.assertIs(reader(model), value, model)
                        else:
                            self.assertEqual(reader(model), value, model)

    def test_set_extra_model_sets_reads_and_clears_value(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir, settings = Path(directory) / "data", Path(directory) / "data" / "settings.json"
            data_dir.mkdir()
            settings.write_text(
                json.dumps({"category": "Pools", "unrelated": "keep"}),
                encoding="utf-8",
            )
            with patch.object(tui, "SETTINGS_FILE", settings), patch.object(tui, "DATA_DIR", data_dir):
                set_extra_model("gpt_fast_model", "provider/model")
                self.assertEqual(
                    json.loads(settings.read_text(encoding="utf-8")),
                    {
                        "category": "Pools",
                        "unrelated": "keep",
                        "gpt_fast_model": "provider/model",
                    },
                )

                set_extra_model("gpt_fast_model", None)
                self.assertEqual(
                    json.loads(settings.read_text(encoding="utf-8")),
                    {"category": "Pools", "unrelated": "keep"},
                )

    def test_saves_leave_no_temporary_files(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir, settings = Path(directory) / "data", Path(directory) / "data" / "settings.json"
            data_dir.mkdir()
            settings.write_text('{"category": "Pools"}', encoding="utf-8")

            with (
                patch.object(tui, "SETTINGS_FILE", settings),
                patch.object(tui, "DATA_DIR", data_dir),
            ):
                set_extra_model("gpt_fast_model", "fast/model")
                set_extra_model("gpt_medium_model", "medium/model")

            saved = json.loads(settings.read_text(encoding="utf-8"))
            temporary_files = list(data_dir.glob("*.tmp"))

        self.assertEqual(temporary_files, [])
        self.assertEqual(
            saved,
            {
                "category": "Pools",
                "gpt_fast_model": "fast/model",
                "gpt_medium_model": "medium/model",
            },
        )

    def test_concurrent_public_saves_preserve_all_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir, settings = Path(directory) / "data", Path(directory) / "data" / "settings.json"
            data_dir.mkdir()
            original = {"category": "Pools"}
            settings.write_text(json.dumps(original), encoding="utf-8")
            barrier = threading.Barrier(2)
            errors = []

            def save(key, value):
                try:
                    barrier.wait()
                    set_extra_model(key, value)
                except BaseException as error:
                    errors.append(error)

            with patch.object(tui, "SETTINGS_FILE", settings), patch.object(tui, "DATA_DIR", data_dir):
                threads = [
                    threading.Thread(target=save, args=("gpt_fast_model", "fast/model")),
                    threading.Thread(target=save, args=("gpt_medium_model", "medium/model")),
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
                saved = json.loads(settings.read_text(encoding="utf-8"))
                temporary_files = list(data_dir.glob("*.tmp"))

        self.assertEqual(errors, [])
        self.assertEqual(temporary_files, [])
        self.assertEqual(
            saved,
            {
                **original,
                "gpt_fast_model": "fast/model",
                "gpt_medium_model": "medium/model",
            },
        )

    def test_windows_lock_helpers_lock_byte_at_offset_zero(self) -> None:
        class LockFile:
            def __init__(self) -> None:
                self.offsets = []
                self.fd_calls = 0
                self.writes = []

            def seek(self, offset):
                self.offsets.append(offset)

            def fileno(self):
                self.fd_calls += 1
                return 7

            def write(self, value):
                self.writes.append(value)
                raise AssertionError("lock files must not be written")

        lock_file = LockFile()
        windows_locking = Mock()
        windows_locking.LK_LOCK = 1
        windows_locking.LK_UNLCK = 2
        windows_locking.locking = Mock()
        with patch.object(tui.os, "name", "nt"), patch.object(tui, "import_module", return_value=windows_locking):
            tui._lock_file(lock_file)
            tui._unlock_file(lock_file)

        self.assertEqual(lock_file.offsets, [0, 0])
        self.assertEqual(lock_file.fd_calls, 2)
        self.assertEqual(lock_file.writes, [])
        self.assertEqual(
            windows_locking.locking.call_args_list,
            [call(7, 1, 1), call(7, 2, 1)],
        )

    def test_cross_process_writes_serialize_without_lost_updates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir, settings = Path(directory) / "data", Path(directory) / "data" / "settings.json"
            data_dir.mkdir()
            settings.write_text(json.dumps({"category": "Pools", "unrelated": "keep"}), encoding="utf-8")
            context = multiprocessing.get_context()
            barrier = context.Barrier(2)
            results = context.Queue()
            processes = [
                context.Process(
                    target=_write_settings_in_process,
                    args=(settings, key, value, barrier, results),
                )
                for key, value in (
                    ("gpt_fast_model", "fast/model"),
                    ("gpt_medium_model", "medium/model"),
                )
            ]
            for process in processes:
                process.start()
            for process in processes:
                process.join(5)
            for process in processes:
                if process.is_alive():
                    process.terminate()
            for process in processes:
                process.join(2)
            self.assertTrue(all(not process.is_alive() for process in processes))
            self.assertTrue(all(process.exitcode == 0 for process in processes))
            outcomes = [results.get(timeout=2) for _ in processes]
            saved = json.loads(settings.read_text(encoding="utf-8"))

        self.assertEqual(sorted(outcomes), [("ok", "gpt_fast_model"), ("ok", "gpt_medium_model")])
        self.assertEqual(
            saved,
            {
                "category": "Pools",
                "unrelated": "keep",
                "gpt_fast_model": "fast/model",
                "gpt_medium_model": "medium/model",
            },
        )

    def test_external_edit_after_failed_load_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir, settings = Path(directory) / "data", Path(directory) / "data" / "settings.json"
            data_dir.mkdir()
            external = {"category": "Grok", "gpt_medium_model": "external/model"}
            settings.write_text(json.dumps(external), encoding="utf-8")
            real_read = tui._read_settings_source
            read_count = 0

            def fail_then_externally_edit():
                nonlocal read_count
                read_count += 1
                if read_count == 1:
                    raise OSError("transient read failure")
                settings.write_text(json.dumps(external), encoding="utf-8")
                return real_read()

            with (
                patch.object(tui, "SETTINGS_FILE", settings),
                patch.object(tui, "DATA_DIR", data_dir),
                patch.object(tui, "_read_settings_source", fail_then_externally_edit),
            ):
                with self.assertRaisesRegex(tui.SettingsSaveConflictError, "refusing to overwrite"):
                    set_extra_model("gpt_fast_model", "local/model")

            saved = json.loads(settings.read_text(encoding="utf-8"))
            temporary_files = list(data_dir.glob("*.tmp"))

        self.assertEqual(read_count, 2)
        self.assertEqual(saved, external)
        self.assertEqual(temporary_files, [])

    def test_a_malformed_source_is_recovered(self):
        corrupt = b"not json"

        def replace_with_different_content(settings: Path) -> None:
            settings.write_bytes(b'{"category":"All"}')

        def change_one_byte_in_place(settings: Path) -> None:
            with settings.open("r+b") as settings_file:
                settings_file.seek(-1, os.SEEK_END)
                settings_file.write(b"X")

        def replace_with_identical_bytes(settings: Path) -> None:
            replacement = settings.with_name("replacement.json")
            replacement.write_bytes(corrupt)
            replacement.replace(settings)

        cases = (
            ("non-utf8", b'\xff\xfe{"model_settings":{}}', None, None),
            ("integer digit limit", ('{"category":' + "9" * 5000 + "}").encode(), None, None),
            ("recursive json", ('{"a":' * 10000 + "1" + "}" * 10000).encode(), None, None),
            ("changed after the read", corrupt, replace_with_different_content, b'{"category":"All"}'),
            ("one byte changed in place", corrupt, change_one_byte_in_place, b"not jsoX"),
            ("replaced with identical bytes", corrupt, replace_with_identical_bytes, corrupt),
        )
        real_read = tui._read_settings_source

        def read_then_perturbing(mutate):
            def read_then():
                source = real_read()
                if mutate is not None:
                    mutate(settings)
                return source
            return read_then

        for name, original, after_read, surviving in cases:
            with self.subTest(source=name), tempfile.TemporaryDirectory() as directory:
                data_dir = Path(directory) / "data"
                settings = data_dir / "settings.json"
                data_dir.mkdir()
                settings.write_bytes(original)

                with (
                    patch.object(tui, "SETTINGS_FILE", settings),
                    patch.object(tui, "DATA_DIR", data_dir),
                    patch.object(
                        tui, "_read_settings_source",
                        side_effect=read_then_perturbing(after_read),
                    ),
                ):
                    loaded, _ = tui._load_settings()

                source = settings.read_bytes()
                backups = [path.read_bytes() for path in data_dir.glob("settings.broken_*.json")]

            self.assertEqual(loaded, {})
            self.assertEqual(source, original if surviving is None else surviving)
            self.assertEqual(backups, [original])

    def test_oversized_settings_keep_the_picker_usable_when_a_write_is_refused(self):
        original = b'{"category":"All","padding":"' + b"x" * 64 + b'"}'

        result, source, session, published = _drive_picker(
            ["Keys.ControlS", "Keys.ControlS", "Keys.ControlM"],
            original,
            _MAX_SETTINGS_BYTES=32,
        )
        screen = session.screens[-1]

        self.assertEqual(result.action, "launch")
        self.assertIn("too large", screen)
        self.assertEqual(screen.count("not saved:"), 1)
        self._assert_roles_row(screen)
        self.assertEqual(source, original)
        self.assertEqual(published, [".settings.json.lock", "settings.json"])

    def test_picker_paints_a_backup_made_by_its_own_write(self):
        _, saved, session, published = _drive_picker(
            ["Keys.ControlS"],
            {"category": "All"},
            during=lambda settings: settings.write_bytes(b"not json"),
        )
        screen = session.screens[-1]
        backups = [name for name in published if name.startswith("settings.broken_")]

        self.assertEqual(session.screens[0].count("was corrupt"), 0)
        self.assertEqual(screen.count("was corrupt"), 1)
        self._assert_shows(screen, backups[0])
        self._assert_roles_row(screen)
        self.assertEqual(
            json.loads(saved),
            {"skip_permissions": True, "last_model": "provider/model"},
        )

    def test_corrupt_backup_is_painted_by_the_first_picker_session_only(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir, settings = Path(directory) / "data", Path(directory) / "data" / "settings.json"
            data_dir.mkdir()
            original = b"not json"
            settings.write_bytes(original)
            painted = [
                _run_picker_session(tui, data_dir, settings, data_dir / "missing.json")
                for _ in range(3)
            ]
            backups = [path.read_bytes() for path in data_dir.glob("settings.broken_*.json")]

        self.assertEqual([screen.count("was corrupt") for screen in painted], [1, 0, 0])
        self._assert_shows(painted[0], f"settings.broken_{hashlib.sha256(original).hexdigest()}.json")
        self.assertEqual(backups, [original])

    def test_oversized_warning_is_painted_by_every_picker_session(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir, settings = Path(directory) / "data", Path(directory) / "data" / "settings.json"
            data_dir.mkdir()
            original = b'{"category":"All","padding":"' + b"x" * 64 + b'"}'
            settings.write_bytes(original)
            with (
                patch.object(tui, "_MAX_SETTINGS_BYTES", 32),
                patch.object(tui, "_read_settings_source", wraps=tui._read_settings_source) as read,
            ):
                painted = [
                    _run_picker_session(tui, data_dir, settings, data_dir / "missing.json")
                    for _ in range(3)
                ]
            source = settings.read_bytes()
            backups = list(data_dir.glob("settings.broken_*.json"))

        self.assertEqual([screen.count("too large") for screen in painted], [1, 1, 1])
        for screen in painted:
            self._assert_shows(
                screen,
                "settings.json is too large; starting with defaults."
                " Move or reduce it to restore your settings",
            )
            self._assert_roles_row(screen)
        self.assertEqual(read.call_count, 3)
        self.assertEqual(source, original)
        self.assertEqual(backups, [])

    def test_oversized_read_carries_no_source_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "settings.json"
            original = b"x" * 64
            settings.write_bytes(original)
            with (
                patch.object(tui, "SETTINGS_FILE", settings),
                patch.object(tui, "_MAX_SETTINGS_BYTES", 32),
                self.assertRaises(tui._SettingsOversizedError) as raised,
            ):
                tui._read_settings_source()

            source = settings.read_bytes()
            published = sorted(path.name for path in Path(directory).iterdir())

        self.assertEqual(vars(raised.exception), {})
        self.assertEqual(raised.exception.args, ())
        self.assertEqual(source, original)
        self.assertEqual(published, ["settings.json"])

    def test_oversized_source_mutation_after_failed_read_is_not_re_read_or_backed_up(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir, settings = Path(directory) / "data", Path(directory) / "data" / "settings.json"
            data_dir.mkdir()
            original = b"x" * 64
            mutated = b"y" * 64
            settings.write_bytes(original)
            real_read = tui._read_settings_source

            def read_then_mutate():
                try:
                    return real_read()
                except tui._SettingsOversizedError:
                    settings.write_bytes(mutated)
                    raise

            with (
                patch.object(tui, "SETTINGS_FILE", settings),
                patch.object(tui, "DATA_DIR", data_dir),
                patch.object(tui, "_MAX_SETTINGS_BYTES", 32),
                patch.object(tui, "_read_settings_source", side_effect=read_then_mutate) as read,
            ):
                loaded, warnings = tui._load_settings()

            source = settings.read_bytes()
            backups = list(data_dir.glob("settings.broken_*.json"))
            temporary_files = list(data_dir.glob("*.tmp"))

        self.assertEqual(loaded, {})
        self.assertEqual(len(warnings), 1)
        self.assertEqual(source, mutated)
        self.assertEqual(read.call_count, 1)
        self.assertEqual(backups, [])
        self.assertEqual(temporary_files, [])

    def test_external_edit_between_corrupt_recovery_and_save_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir, settings = Path(directory) / "data", Path(directory) / "data" / "settings.json"
            data_dir.mkdir()
            settings.write_bytes(b"not json")
            external = {"category": "Grok", "gpt_medium_model": "external/model"}

            def save_over_an_external_edit(current: dict) -> None:
                settings.write_text(json.dumps(external), encoding="utf-8")
                current["gpt_fast_model"] = "local/model"

            with (
                patch.object(tui, "SETTINGS_FILE", settings),
                patch.object(tui, "DATA_DIR", data_dir),
                self.assertRaisesRegex(tui.SettingsSaveConflictError, "refusing to overwrite"),
            ):
                tui._update_settings(save_over_an_external_edit)

            saved = json.loads(settings.read_text(encoding="utf-8"))
            temporary_files = list(data_dir.glob("*.tmp"))

        self.assertEqual(saved, external)
        self.assertEqual(temporary_files, [])

    def test_repair_write_after_corrupt_recovery_replaces_the_backed_up_source(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir, settings = Path(directory) / "data", Path(directory) / "data" / "settings.json"
            data_dir.mkdir()
            original = b"not json"
            settings.write_bytes(original)
            with patch.object(tui, "SETTINGS_FILE", settings), patch.object(tui, "DATA_DIR", data_dir):
                loaded, _ = tui._load_settings()
                set_extra_model("gpt_fast_model", "fast/model")

            saved = json.loads(settings.read_text(encoding="utf-8"))
            backups = [path.read_bytes() for path in data_dir.glob("settings.broken_*.json")]

        self.assertEqual(loaded, {})
        self.assertEqual(saved, {"gpt_fast_model": "fast/model"})
        self.assertEqual(backups, [original])

    def test_public_save_creates_settings_that_do_not_exist_yet(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir, settings = Path(directory) / "data", Path(directory) / "data" / "settings.json"
            data_dir.mkdir()
            with (
                patch.object(tui, "SETTINGS_FILE", settings),
                patch.object(tui, "DATA_DIR", data_dir),
                patch.object(tui, "SETTINGS_EXAMPLE_FILE", data_dir / "missing.json"),
            ):
                set_extra_model("gpt_fast_model", "fast/model")
            saved = json.loads(settings.read_text(encoding="utf-8"))
            temporary_files = list(data_dir.glob("*.tmp"))

        self.assertEqual(saved, {"gpt_fast_model": "fast/model"})
        self.assertEqual(temporary_files, [])

    def test_oversized_load_refuses_to_overwrite_the_file_it_could_not_read(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir, settings = Path(directory) / "data", Path(directory) / "data" / "settings.json"
            data_dir.mkdir()
            original = b'{"category":"All","padding":"' + b"x" * 64 + b'"}'
            settings.write_bytes(original)
            with (
                patch.object(tui, "SETTINGS_FILE", settings),
                patch.object(tui, "DATA_DIR", data_dir),
                patch.object(tui, "_MAX_SETTINGS_BYTES", 32),
                self.assertRaisesRegex(tui.SettingsSaveConflictError, "refusing to overwrite"),
            ):
                set_extra_model("gpt_fast_model", "fast/model")

            saved = settings.read_bytes()
            temporary_files = list(data_dir.glob("*.tmp"))

        self.assertEqual(saved, original)
        self.assertEqual(temporary_files, [])

    def test_settings_reader_peak_memory_stays_near_the_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "settings.json"
            settings.write_bytes(b"x" * (8 * 1024 * 1024))
            with (
                patch.object(tui, "SETTINGS_FILE", settings),
                patch.object(tui, "_MAX_SETTINGS_BYTES", 64 * 1024),
            ):
                tracemalloc.start()
                try:
                    loaded, warnings = tui._load_settings()
                    peak = tracemalloc.get_traced_memory()[1]
                finally:
                    tracemalloc.stop()
            source_size = settings.stat().st_size

        self.assertEqual(loaded, {})
        self.assertEqual(len(warnings), 1)
        self.assertEqual(source_size, 8 * 1024 * 1024)
        self.assertLess(peak, 4 * 64 * 1024)

    def test_backup_comparison_peak_memory_stays_within_the_settings_cap(self):
        cap = 256 * 1024
        corrupt = b"x" * cap
        backup_name = f"settings.broken_{hashlib.sha256(corrupt).hexdigest()}.json"
        with tempfile.TemporaryDirectory() as directory:
            data_dir, settings = Path(directory) / "data", Path(directory) / "data" / "settings.json"
            data_dir.mkdir()
            settings.write_bytes(corrupt)
            (data_dir / backup_name).write_bytes(corrupt)
            with (
                patch.object(tui, "SETTINGS_FILE", settings),
                patch.object(tui, "DATA_DIR", data_dir),
                patch.object(tui, "_MAX_SETTINGS_BYTES", cap),
            ):
                tracemalloc.start()
                try:
                    loaded, warnings = tui._load_settings()
                    peak = tracemalloc.get_traced_memory()[1]
                finally:
                    tracemalloc.stop()
            source = settings.stat().st_size

        self.assertEqual(loaded, {})
        self.assertEqual(warnings, ())
        self.assertEqual(source, cap)
        self.assertLess(peak, 8 * cap)

    def test_reader_admits_bytes_up_to_the_cap_and_warns_past_it(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir, settings = Path(directory) / "data", Path(directory) / "data" / "settings.json"
            data_dir.mkdir()
            at_cap = b'{"category":"All","pad":"abcde"}'
            over_cap = b'{"category":"All","pad":"abcdef"}'
            settings.write_bytes(at_cap)
            with (
                patch.object(tui, "SETTINGS_FILE", settings),
                patch.object(tui, "DATA_DIR", data_dir),
                patch.object(tui, "_MAX_SETTINGS_BYTES", 32),
            ):
                loaded, warnings = tui._load_settings()
                settings.write_bytes(over_cap)
                oversized_loaded, oversized_warnings = tui._load_settings()

            source = settings.read_bytes()
            backups = list(data_dir.glob("settings.broken_*.json"))
            temporary_files = list(data_dir.glob("*.tmp"))

        self.assertEqual(loaded, {"category": "All", "pad": "abcde"})
        self.assertEqual(warnings, ())
        self.assertEqual(oversized_loaded, {})
        self.assertEqual(
            oversized_warnings,
            ("settings.json is too large; starting with defaults. Move or reduce it to restore your settings",),
        )
        self.assertEqual(source, over_cap)
        self.assertEqual(backups, [])
        self.assertEqual(temporary_files, [])

    def test_a_rejected_settings_value_leaves_the_file_untouched(self) -> None:
        cases = (
            ("above the reader cap", "too large", {"_MAX_SETTINGS_BYTES": 32}, "x" * 32),
            ("a terminal control character", "control", {}, "provider/model\x1b[2J"),
        )
        for name, message, patches, value in cases:
            with self.subTest(rejected=name), tempfile.TemporaryDirectory() as directory:
                data_dir = Path(directory) / "data"
                settings = data_dir / "settings.json"
                data_dir.mkdir()
                original = '{"category":"All"}'
                settings.write_text(original, encoding="utf-8")
                with ExitStack() as stack:
                    stack.enter_context(patch.object(tui, "SETTINGS_FILE", settings))
                    stack.enter_context(patch.object(tui, "DATA_DIR", data_dir))
                    for attribute, patched in patches.items():
                        stack.enter_context(patch.object(tui, attribute, patched))
                    stack.enter_context(self.assertRaisesRegex(ValueError, message))
                    set_extra_model("gpt_fast_model", value)

                self.assertEqual(settings.read_text(encoding="utf-8"), original)
                self.assertEqual(list(data_dir.glob("*.tmp")), [])

    def test_model_parameter_editor_refuses_before_prompting_when_settings_cannot_be_read(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir, settings = Path(directory) / "data", Path(directory) / "data" / "settings.json"
            data_dir.mkdir()
            original = b'{"category":"All","padding":"' + b"x" * 64 + b'"}'
            settings.write_bytes(original)
            printed = io.StringIO()
            with (
                patch.object(tui, "SETTINGS_FILE", settings),
                patch.object(tui, "DATA_DIR", data_dir),
                patch.object(tui, "SETTINGS_EXAMPLE_FILE", data_dir / "missing.json"),
                patch.object(tui, "_MAX_SETTINGS_BYTES", 32),
                patch("builtins.input", side_effect=AssertionError("must not prompt")),
                redirect_stdout(printed),
            ):
                configure_model_parameters("provider/model")

            source = settings.read_bytes()
            residue = list(data_dir.glob("*.tmp"))

        self.assertIn("too large", printed.getvalue())
        self.assertIn("not saved: settings.json is not the file Claudex last read", printed.getvalue())
        self.assertNotIn("Press Enter to keep a value", printed.getvalue())
        self.assertEqual(source, original)
        self.assertEqual(residue, [])

    def test_unreadable_settings_warn_instead_of_failing_silently(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "settings.json"
            settings.write_bytes(b'{"category": "All"}')
            with (
                patch.object(tui, "SETTINGS_FILE", settings),
                patch.object(tui, "_read_settings_source", side_effect=PermissionError("denied")),
            ):
                loaded, warnings = tui._load_settings()

        self.assertEqual(loaded, {})
        self.assertEqual(len(warnings), 1)
        self.assertIn("could not be read", warnings[0])

    def test_non_object_json_is_backed_up_before_a_public_write(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir, settings = Path(directory) / "data", Path(directory) / "data" / "settings.json"
            data_dir.mkdir()
            original = b'[{"category": "All"}]\n'
            settings.write_bytes(original)
            with (
                patch.object(tui, "SETTINGS_FILE", settings),
                patch.object(tui, "DATA_DIR", data_dir),
            ):
                set_extra_model("gpt_fast_model", "fast/model")
            backup = data_dir / f"settings.broken_{hashlib.sha256(original).hexdigest()}.json"
            self.assertEqual(backup.read_bytes(), original)
            self.assertEqual(
                json.loads(settings.read_text(encoding="utf-8")),
                {"gpt_fast_model": "fast/model"},
            )

    def test_a_backup_path_already_taken_is_diagnosed_not_overwritten(self):
        corrupt = b"not json"
        cases = (
            ("holds different settings content", b"different recovery", {},
             "holds different settings content", "larger than the settings limit",
             [b"different recovery"]),
            ("is larger than the settings limit", b"y" * 512, {"_MAX_SETTINGS_BYTES": 64},
             "larger than the settings limit", "holds different settings content",
             [b"y" * 512]),
        )
        for name, existing_bytes, patches, diagnosis, ruled_out, expected in cases:
            with self.subTest(backup=name), tempfile.TemporaryDirectory() as directory:
                data_dir = Path(directory) / "data"
                settings = data_dir / "settings.json"
                data_dir.mkdir()
                settings.write_bytes(corrupt)
                existing = data_dir / f"settings.broken_{hashlib.sha256(corrupt).hexdigest()}.json"
                existing.write_bytes(existing_bytes)
                raised = self.assertRaises(tui.SettingsRecoveryExhaustedError)
                with ExitStack() as stack:
                    stack.enter_context(patch.object(tui, "SETTINGS_FILE", settings))
                    stack.enter_context(patch.object(tui, "DATA_DIR", data_dir))
                    for attribute, patched in patches.items():
                        stack.enter_context(patch.object(tui, attribute, patched))
                    stack.enter_context(raised)
                    tui._load_settings()
                source = settings.read_bytes()
                backups = [p.read_bytes() for p in data_dir.glob("settings.broken_*.json")]

            self.assertIn(existing.name, str(raised.exception))
            self.assertIn(diagnosis, str(raised.exception))
            self.assertIn("move or delete", str(raised.exception))
            self.assertNotIn(ruled_out, str(raised.exception))
            self.assertEqual(source, corrupt)
            self.assertEqual(backups, expected)

    def test_backup_reservation_does_not_overwrite_replaced_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir, settings = Path(directory) / "data", Path(directory) / "data" / "settings.json"
            data_dir.mkdir()
            original = b"not json"
            settings.write_bytes(original)
            real_link = os.link
            inserted_destinations = []

            def insert_destination_then_link(source_path, destination, *, follow_symlinks=True):
                candidate = Path(destination)
                candidate.write_bytes(b"external backup")
                inserted_destinations.append(candidate)
                return real_link(source_path, destination, follow_symlinks=follow_symlinks)

            with (
                patch.object(tui, "SETTINGS_FILE", settings),
                patch.object(tui, "DATA_DIR", data_dir),
                patch.object(tui.os, "link", side_effect=insert_destination_then_link),
            ):
                with self.assertRaises(tui.SettingsRecoveryExhaustedError):
                    tui._load_settings()[0]
            source = settings.read_bytes()
            backup_contents = [path.read_bytes() for path in inserted_destinations]

        self.assertEqual(len(inserted_destinations), 1)
        self.assertEqual(source, original)
        self.assertEqual(backup_contents, [b"external backup"])

    def test_corrupt_backup_is_reported_once_and_not_re_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir, settings = Path(directory) / "data", Path(directory) / "data" / "settings.json"
            data_dir.mkdir()
            original = b"not json"
            settings.write_bytes(original)
            with patch.object(tui, "SETTINGS_FILE", settings), patch.object(tui, "DATA_DIR", data_dir):
                first_load, first_warnings = tui._load_settings()
                second_load, second_warnings = tui._load_settings()
            source = settings.read_bytes()
            backups = list(data_dir.glob("settings.broken_*.json"))

        self.assertEqual(first_load, {})
        self.assertEqual(
            first_warnings,
            (
                "settings.json was corrupt; backed up to "
                f"settings.broken_{hashlib.sha256(original).hexdigest()}.json",
            ),
        )
        self.assertEqual(second_load, {})
        self.assertEqual(second_warnings, ())
        self.assertEqual(len(backups), 1)
        self.assertEqual(source, original)

    def test_backup_fsync_failure_removes_partial_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir, settings = Path(directory) / "data", Path(directory) / "data" / "settings.json"
            data_dir.mkdir()
            original = b"not json"
            settings.write_bytes(original)

            def fail_fsync(descriptor):
                raise OSError("fsync failed")

            with (
                patch.object(tui, "SETTINGS_FILE", settings),
                patch.object(tui, "DATA_DIR", data_dir),
                patch.object(tui.os, "fsync", side_effect=fail_fsync),
                self.assertRaises(tui.SettingsRecoveryExhaustedError),
            ):
                tui._load_settings()
            source = settings.read_bytes()
            backups = list(data_dir.glob("settings.broken_*.json"))
            temporary_files = list(data_dir.glob("*.tmp"))

        self.assertEqual(source, original)
        self.assertEqual(backups, [])
        self.assertEqual(temporary_files, [])


class PickerDeltaTests(unittest.TestCase):
    def _assert_shows(self, screen: str, text: str) -> None:
        self.assertIn("".join(text.split()), "".join(screen.split()))

    def _assert_roles_row(
        self, screen: str, fast: str = "default", medium: str = "default", subagent: str = "default"
    ) -> None:
        rows = [row for row in screen.splitlines() if "Fast:" in row]
        self.assertEqual(len(rows), 1, screen)
        squeezed = "".join(rows[0].split())
        for label, value in (("Fast:", fast), ("Medium:", medium), ("Subagent:", subagent)):
            self.assertIn(f"{label}{value}", squeezed)
        self.assertIn("GW:", squeezed)

    def _drive(self, keys, initial, during=None, model=None, **patches):
        result, source, session, _ = _drive_picker(
            keys,
            initial,
            during=during,
            models=[model or Model("provider/model", "provider")],
            **patches,
        )
        return result, source.decode(), session.screens[-1]

    def test_a_toggled_permission_is_written_however_many_times_it_is_toggled(self):
        cases = (
            ("toggled once", ["Keys.ControlS", "Keys.ControlM"], True),
            ("toggled back to its loaded value",
             ["Keys.ControlS", "Keys.ControlS", "Keys.ControlM"], False),
        )
        for name, keys, toggled in cases:
            with self.subTest(control_s=name):
                result, source, screen = self._drive(
                    keys, {"category": "All", "skip_permissions": False}
                )

                self.assertIs(json.loads(source)["skip_permissions"], toggled)
                self.assertIs(result.skip_permissions, toggled)
                self._assert_shows(screen, "Dangerous: ON" if toggled else "Dangerous: OFF")
                self.assertNotIn("not written", screen)
                self._assert_roles_row(screen)

    def test_a_field_changed_or_removed_on_disk_is_disclosed(self):
        cases = (
            ("changed", {"category": "Codex"}, "Codex"),
            ("removed", {"unrelated": "keep"}, None),
        )
        for name, external, expected in cases:
            with self.subTest(field=name):
                _, source, screen = self._drive(
                    ["Keys.ControlS"],
                    {"category": "Grok"},
                    during=lambda settings, external=external: settings.write_text(
                        json.dumps(external)
                    ),
                    model=Model("xai/grok-4", "xai"),
                )

                if expected is None:
                    self.assertNotIn("category", json.loads(source))
                else:
                    self.assertEqual(json.loads(source)["category"], expected)
                self._assert_shows(screen, "category changed on disk while Claudex was open")
                self._assert_shows(screen, "the value on this screen was not written")
                self._assert_roles_row(screen)

    def test_a_save_over_a_corrupt_source_blames_the_corruption_not_a_field(self):
        corrupt = b"not json"
        _, source, screen = self._drive(
            ["Keys.ControlS"],
            {"category": "Grok", "skip_permissions": True},
            during=lambda settings: settings.write_bytes(corrupt),
            model=Model("xai/grok-4", "xai"),
        )

        self.assertEqual(
            json.loads(source), {"skip_permissions": False, "last_model": "xai/grok-4"}
        )
        self._assert_shows(screen, f"settings.broken_{hashlib.sha256(corrupt).hexdigest()}.json")
        self.assertNotIn("changed on disk while Claudex was open", screen)
        self.assertNotIn("not saved", screen)

    def test_recovery_preserves_a_field_hand_edited_while_the_picker_was_open(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir, settings = Path(directory) / "data", Path(directory) / "data" / "settings.json"
            data_dir.mkdir()
            corrupt = b"not json"
            settings.write_bytes(corrupt)
            hand_edited = json.dumps({"category": "Grok"}).encode()

            class HandEditedAfterRecovery(_PickerSession):
                def run(self):
                    settings.write_bytes(hand_edited)
                    return super().run()

            build, sessions = _picker_sessions(["Keys.ControlS"], HandEditedAfterRecovery)
            with (
                patch.object(tui, "Application", build),
                patch.object(tui, "SETTINGS_FILE", settings),
                patch.object(tui, "DATA_DIR", data_dir),
                patch.object(tui, "SETTINGS_EXAMPLE_FILE", data_dir / "missing.json"),
                patch("modules.router_starter.router_is_ready", return_value=False),
            ):
                tui.run_picker([Model("provider/model", "provider")])

            saved = json.loads(settings.read_text(encoding="utf-8"))
            backups = [path.read_bytes() for path in data_dir.glob("settings.broken_*.json")]
            screen = sessions[0].screens[-1]

        self.assertEqual(
            saved,
            {"category": "Grok", "skip_permissions": True, "last_model": "provider/model"},
        )
        self.assertEqual(backups, [corrupt])
        self._assert_shows(screen, f"settings.broken_{hashlib.sha256(corrupt).hexdigest()}.json")
        self._assert_roles_row(screen)

    def test_hand_edited_field_survives_while_the_toggled_field_is_written(self):
        _, saved, screen = self._drive(
            ["Keys.ControlS"],
            {"category": "Grok", "gpt_fast_model": "x/fast", "gpt_medium_model": "x/medium"},
            during=lambda settings: settings.write_text(json.dumps(
                {"category": "Codex", "gpt_fast_model": "x/fast", "gpt_medium_model": "hand/model"}
            ), encoding="utf-8"),
            model=Model("xai/grok-4", "xai"),
        )

        self.assertEqual(
            json.loads(saved),
            {
                "category": "Codex",
                "gpt_medium_model": "hand/model",
                "gpt_fast_model": "x/fast",
                "skip_permissions": True,
                "last_model": "xai/grok-4",
            },
        )
        self.assertEqual(screen.count("not saved:"), 0)
        self._assert_shows(screen, "Filter: Grok")
        self._assert_roles_row(screen, fast="x/fast", medium="x/medium")

    def test_empty_delta_leaves_the_settings_file_unwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir, settings = Path(directory) / "data", Path(directory) / "data" / "settings.json"
            data_dir.mkdir()
            original = json.dumps({"category": "All", "last_model": "provider/model"}, indent=2) + "\n"
            settings.write_text(original, encoding="utf-8")
            before = settings.stat()

            build, sessions = _picker_sessions(["Keys.Escape"])
            with (
                patch.object(tui, "Application", build),
                patch.object(tui, "SETTINGS_FILE", settings),
                patch.object(tui, "DATA_DIR", data_dir),
                patch.object(tui, "SETTINGS_EXAMPLE_FILE", data_dir / "missing.json"),
                patch("modules.router_starter.router_is_ready", return_value=False),
            ):
                result = tui.run_picker([Model("provider/model", "provider")])

            after = settings.stat()
            published = sorted(path.name for path in data_dir.iterdir())
            source = settings.read_text(encoding="utf-8")

        self.assertEqual(result.action, "exit")
        self.assertEqual(source, original)
        self.assertEqual(after.st_ino, before.st_ino)
        self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(published, [".settings.json.lock", "settings.json"])

    def test_a_shifted_category_is_written(self):
        result, source, screen = self._drive(
            ["Keys.ControlI", "Keys.ControlM"],
            {"category": "All", "last_model": "pool/shared"},
            model=Model("pool/shared", "pool", True),
        )

        self.assertEqual(json.loads(source)["category"], "Pools")
        self.assertEqual(json.loads(source)["last_model"], "pool/shared")
        self.assertNotIn("skip_permissions", json.loads(source))
        self._assert_shows(screen, "Filter: Pools")
        self._assert_roles_row(screen)

    def test_a_selected_model_is_written(self):
        result, source, screen = self._drive(
            ["Keys.ControlM"],
            {
                "category": "All",
                "model_settings": {
                    "provider/model": {"context_tokens": 200000, "auto_compact": True}
                },
            },
        )

        self.assertEqual(json.loads(source)["last_model"], "provider/model")
        self.assertEqual(json.loads(source)["category"], "All")
        self.assertNotIn("skip_permissions", json.loads(source))
        self.assertEqual((result.context_tokens, result.auto_compact), (200000, True))
        self._assert_roles_row(screen)

    def test_a_picker_changed_field_is_refused_while_the_file_cannot_be_read(self):
        result, source, screen = self._drive(
            ["Keys.ControlS"],
            {"category": "Grok", "padding": "x" * 128},
            during=lambda settings: settings.write_text(
                json.dumps({"category": "Codex", "padding": "y" * 128})
            ),
            _MAX_SETTINGS_BYTES=96,
        )

        self.assertEqual(json.loads(source), {"category": "Codex", "padding": "y" * 128})
        self.assertEqual(screen.count("not saved:"), 1)
        self.assertNotIn("not written", screen)

    def test_a_refused_toggle_is_retried_once_the_file_can_be_read(self):
        real_read = tui._read_settings_source
        reads = 0

        def read_then_fail_once():
            nonlocal reads
            reads += 1
            if reads == 2:
                raise OSError("transient read failure")
            return real_read()

        result, source, screen = self._drive(
            ["Keys.ControlS", "Keys.Escape"],
            {"category": "All", "skip_permissions": False, "last_model": "provider/model"},
            _read_settings_source=read_then_fail_once,
        )

        self.assertIs(json.loads(source)["skip_permissions"], True)
        self._assert_shows(screen, "Dangerous: ON")

    def test_an_oversized_file_leaves_the_model_readers_at_their_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir, settings = Path(directory) / "data", Path(directory) / "data" / "settings.json"
            data_dir.mkdir()
            original = b'{"model_settings":{"provider/model":{"context_tokens":200000,"auto_compact":true}}}'
            settings.write_bytes(original)
            with (
                patch.object(tui, "SETTINGS_FILE", settings),
                patch.object(tui, "DATA_DIR", data_dir),
                patch.object(tui, "_MAX_SETTINGS_BYTES", 32),
            ):
                self.assertIsNone(get_model_context("provider/model"))
                self.assertIsNone(get_model_autocompact("provider/model"))
                loaded, warnings = tui._load_settings()
            source = settings.read_bytes()

        self.assertEqual(loaded, {})
        self.assertEqual(len(warnings), 1)
        self.assertIn("too large", warnings[0])
        self.assertEqual(source, original)


class PickerScreenTests(unittest.TestCase):
    def _assert_shows(self, screen: str, text: str) -> None:
        self.assertIn("".join(text.split()), "".join(screen.split()))

    def test_the_footer_renders_the_same_text_at_every_terminal_width(self):
        oracle = _footer_text(_drive_picker(columns=200)[2].screens[-1])
        self.assertNotEqual(oracle, "")

        for columns in (40, 60, 81, 100, 120):
            with self.subTest(columns=columns):
                self.assertEqual(_footer_text(_drive_picker(columns=columns)[2].screens[-1]), oracle)

    def test_the_roles_row_keeps_every_value_at_every_terminal_width(self):
        settings = {
            "gpt_fast_model": "openai/fast-model",
            "gpt_medium_model": "anthropic/medium-model",
            "gpt_subagent_model": "xai/subagent-model",
        }
        for columns in (40, 81, 200):
            with self.subTest(columns=columns):
                screen = _drive_picker(initial=settings, columns=columns)[2].screens[-1]
                self._assert_shows(
                    screen,
                    "Fast: openai/fast-model   Medium: anthropic/medium-model"
                    "   Subagent: xai/subagent-model",
                )

    def test_the_sub_picker_ignores_keys_it_cannot_act_on(self):
        models = [
            Model("openai/gpt-5", "openai"),
            Model("xai/grok-4", "xai"),
            Model("pool/shared", "pool", True),
        ]
        initial = {"category": "All"}
        probe = _drive_picker(sub_picker=True, models=models, initial=initial)[2]
        registered = {
            tuple(str(key) for key in binding.keys)
            for binding in probe.key_bindings.bindings
        }
        footer = _footer_text(probe.screens[-1])

        for token, keys in sorted(_FOOTER_KEYS.items()):
            with self.subTest(key=token):
                if tuple(keys) not in registered:
                    self.assertNotIn(token, footer)
                    continue
                result, _, session, _ = _drive_picker(
                    list(keys), sub_picker=True, models=models, initial=initial
                )
                if result is None:
                    self.assertNotEqual(
                        session.screens[-1], session.screens[0], f"{token} is bound and repainted nothing"
                    )
                else:
                    self.assertIn(
                        result.action,
                        ("launch", "clear", "cancel"),
                        f"{token} reports an action the sub-picker cannot carry",
                    )

    def test_the_sub_picker_reports_a_settings_recovery_it_triggers(self):
        corrupt = b"not json"

        result, source, session, published = _drive_picker(
            ["Keys.Escape"], sub_picker=True, initial=corrupt
        )

        self.assertEqual(result.action, "cancel")
        self.assertEqual(source, corrupt)
        self._assert_shows(
            session.screens[-1], f"settings.broken_{hashlib.sha256(corrupt).hexdigest()}.json"
        )
        self.assertIn(f"settings.broken_{hashlib.sha256(corrupt).hexdigest()}.json", published)

    def test_a_recovery_that_cannot_reserve_its_backup_is_painted_not_raised(self):
        corrupt = b"not json"
        backup_name = f"settings.broken_{hashlib.sha256(corrupt).hexdigest()}.json"

        result, source, session, _ = _drive_picker(
            ["Keys.ControlS"],
            initial=corrupt,
            during=lambda settings: settings.with_name(backup_name).write_bytes(b"y" * 512),
            _MAX_SETTINGS_BYTES=64,
        )

        self.assertIsNone(result)
        self.assertEqual(source, corrupt)
        self._assert_shows(session.screens[-1], "not saved:")
        self._assert_shows(session.screens[-1], f"{backup_name} is larger than the settings limit")

    def test_a_picker_save_above_the_size_cap_is_painted_not_raised(self):
        initial = {"category": "All", "skip_permissions": True, "last_model": "provider/model"}

        result, source, session, _ = _drive_picker(
            ["Keys.ControlS"], initial=initial, _MAX_SETTINGS_BYTES=77
        )

        self.assertIsNone(result)
        self.assertEqual(json.loads(source), initial)
        self._assert_shows(session.screens[-1], "not saved: settings.json is too large")

    def test_a_reported_warning_starts_its_own_rendered_row(self):
        corrupt = b"not json"
        for columns in (40, 81):
            with self.subTest(columns=columns):
                for sub_picker in (False, True):
                    with self.subTest(sub_picker=sub_picker):
                        screen = _drive_picker(
                            ["Keys.Escape"],
                            sub_picker=sub_picker,
                            initial=corrupt,
                            columns=columns,
                        )[2].screens[-1]
                        self.assertTrue(
                            any(
                                row.startswith("settings.json was corrupt")
                                for row in screen.splitlines()
                            ),
                            screen,
                        )

    def test_typing_in_the_search_box_filters_the_rendered_list(self):
        models = [
            Model("openai/gpt-5", "openai"),
            Model("xai/grok-4", "xai"),
            Model("pool/shared", "pool", True),
        ]

        result, _, session, _ = _drive_picker(
            ["grok", "Keys.ControlM"], models=models, initial={"category": "All"}
        )

        self.assertEqual(result.action, "launch")
        self.assertEqual(result.model.id, "xai/grok-4")
        self._assert_shows(session.screens[-1], "grok")
        self.assertNotIn("openai/gpt-5", session.screens[-1])
        self.assertNotIn("pool/shared", session.screens[-1])

    def test_clear_clears_the_search_before_it_clears_the_role(self):
        _, source, session, _ = _drive_picker(
            ["gpt", "Keys.Delete"],
            sub_picker=True,
            models=[Model("openai/gpt-5", "openai"), Model("xai/grok-4", "xai")],
            initial={"gpt_fast_model": "keep/model"},
        )

        self.assertEqual(source, b'{"gpt_fast_model": "keep/model"}')
        self._assert_shows(session.screens[-1], "openai/gpt-5")
        self._assert_shows(session.screens[-1], "xai/grok-4")

    def test_clear_with_no_search_clears_the_role(self):
        result, source, _, _ = _drive_picker(
            ["Keys.Delete"], sub_picker=True, initial={"gpt_fast_model": "keep/model"}
        )

        self.assertEqual(result.action, "clear")
        self.assertEqual(source, b'{"gpt_fast_model": "keep/model"}')

    def test_escape_restores_the_list_after_a_search_that_matched_nothing(self):
        models = [Model("openai/gpt-5", "openai"), Model("xai/grok-4", "xai")]

        result, _, session, _ = _drive_picker(
            ["zzz", "Keys.Escape", "Keys.Escape"], models=models
        )
        before, empty, after = (screen.splitlines() for screen in session.screens[:3])

        self.assertEqual(result.action, "exit")
        self.assertEqual(
            [index for index, row in enumerate(empty) if "No matching models." in row],
            [index for index, row in enumerate(before) if "openai/gpt-5" in row],
        )
        self.assertEqual([row for row in after if "openai/gpt-5" in row], ["  › openai/gpt-5  [openai]"])
        self.assertEqual([row for row in after if "xai/grok-4" in row], ["    xai/grok-4  [xai]"])
        self.assertFalse([row for row in after if "No matching models." in row])

    def test_every_terminal_size_shows_at_least_one_model_row(self):
        corrupt = b"not json"
        for columns, rows in ((40, 12), (81, 24), (200, 50)):
            with self.subTest(columns=columns, rows=rows):
                screen = _drive_picker(
                    initial=corrupt, columns=columns, rows=rows, keys=["Keys.ControlS"]
                )[2].screens[-1]
                self.assertIn("provider/model", screen)


if __name__ == "__main__":
    unittest.main()
