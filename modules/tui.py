from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import TextIO

from prompt_toolkit import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.data_structures import Point
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.styles import Style

from .config import DATA_DIR, SETTINGS_EXAMPLE_FILE, SETTINGS_FILE
from .models import CATEGORIES, Model, filter_models


@dataclass(slots=True)
class PickerResult:
    action: str
    model: Model | None
    skip_permissions: bool
    context_tokens: int | None
    auto_compact: bool | None = None
    gpt_fast_model: str | None = None
    gpt_medium_model: str | None = None
    gpt_subagent_model: str | None = None


def _exit_once(event, result: PickerResult) -> None:
    if not event.app.is_done:
        event.app.exit(result)


def _model_settings_for(model_id: str) -> dict:
    settings, _ = _load_settings()
    return settings.get("model_settings", {}).get(model_id, {})


def get_model_autocompact(model_id: str) -> bool | None:
    return _model_settings_for(model_id).get("auto_compact")


def get_model_context(model_id: str) -> int | None:
    return _model_settings_for(model_id).get("context_tokens")

_SETTINGS_SAVE_LOCK = threading.Lock()
_NO_SETTINGS_DIGEST = ""
_UNREADABLE_SETTINGS_DIGEST = "unreadable"
_SETTINGS_DIGEST = _NO_SETTINGS_DIGEST


class SettingsSaveConflictError(RuntimeError):
    pass


class SettingsRecoveryExhaustedError(RuntimeError):
    pass


class _SettingsReadError(ValueError):
    def __init__(self, message: str, raw: bytes) -> None:
        super().__init__(message)
        self.raw = raw


class _SettingsOversizedError(ValueError):
    pass


def _lock_file(lock_file: TextIO) -> None:
    locking = import_module("msvcrt" if os.name == "nt" else "fcntl")
    lock_file.seek(0)
    if os.name == "nt":
        locking.locking(lock_file.fileno(), locking.LK_LOCK, 1)
    else:
        locking.lockf(lock_file.fileno(), locking.LOCK_EX, 1, 0, os.SEEK_SET)


def _unlock_file(lock_file: TextIO) -> None:
    locking = import_module("msvcrt" if os.name == "nt" else "fcntl")
    lock_file.seek(0)
    if os.name == "nt":
        locking.locking(lock_file.fileno(), locking.LK_UNLCK, 1)
    else:
        locking.lockf(lock_file.fileno(), locking.LOCK_UN, 1, 0, os.SEEK_SET)


@contextmanager
def _settings_transaction() -> Iterator[None]:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_path = SETTINGS_FILE.with_name(f".{SETTINGS_FILE.name}.lock")
    with _SETTINGS_SAVE_LOCK, lock_path.open("a+") as lock_file:
        _lock_file(lock_file)
        try:
            yield
        finally:
            _unlock_file(lock_file)


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


_MAX_SETTINGS_BYTES = 1024 * 1024
_SETTINGS_RECOVERY_ERROR = "settings.json was corrupt; backup could not be created; source preserved"
_SETTINGS_UNREADABLE_WARNING = "settings.json could not be read; Claudex is using defaults"
_SETTINGS_SAVE_CONFLICT_ERROR = (
    "settings.json is not the file Claudex last read; refusing to overwrite it so no edits are lost"
)
_SETTINGS_OUTSIDE_EDIT = "{field} changed on disk while Claudex was open; the value on this screen was not written"
_MODEL_FIELDS = ("context_tokens", "auto_compact")
_PICKER_FIELDS = ("category", "skip_permissions", "last_model")
_PICKER_DEFAULTS: dict[str, str | bool] = {"category": "All", "skip_permissions": False, "last_model": ""}
_SETTINGS_OVERSIZED_WARNING = (
    "settings.json is too large; starting with defaults. Move or reduce it to restore your settings"
)
_LAUNCHER_FOOTER = (
    ("↑↓", "select"),
    ("Enter", "launch"),
    ("Tab", "category"),
    ("F5", "refresh"),
    ("F6", "model roles"),
    ("F7", "pools"),
    ("F8", "CLIProxy UI"),
    ("F9", "gateway"),
    ("F10", "model params"),
    ("Del", "clear"),
    ("Ctrl+S", "permissions"),
    ("Esc", "clear/exit"),
)
_SUB_PICKER_FOOTER = (("↑↓", "select"), ("Enter", "launch"), ("Tab", "category"), ("Del", "clear"), ("Esc", "cancel"))


def _read_settings_source() -> bytes:
    with SETTINGS_FILE.open("rb") as settings_file:
        raw = settings_file.read(_MAX_SETTINGS_BYTES + 1)
    if len(raw) > _MAX_SETTINGS_BYTES:
        raise _SettingsOversizedError
    return raw


def _decode_settings_source(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise _SettingsReadError(str(error), raw) from error


def _backup_corrupt_settings(raw: bytes) -> tuple[str, ...]:
    try:
        descriptor, name = tempfile.mkstemp(
            prefix=f".{SETTINGS_FILE.name}.",
            suffix=".tmp",
            dir=SETTINGS_FILE.parent,
        )
    except OSError as error:
        raise SettingsRecoveryExhaustedError(_SETTINGS_RECOVERY_ERROR) from error
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as backup_file:
            backup_file.write(raw)
            backup_file.flush()
            os.fsync(backup_file.fileno())
        digest = _digest(raw)
        backup_path = SETTINGS_FILE.with_name(f"{SETTINGS_FILE.stem}.broken_{digest}.json")
        try:
            os.link(temporary, backup_path, follow_symlinks=False)
        except FileExistsError as error:
            if backup_path.stat().st_size > _MAX_SETTINGS_BYTES:
                raise SettingsRecoveryExhaustedError(
                    f"{backup_path.name} is larger than the settings limit; move or delete it, then retry"
                ) from error
            if backup_path.read_bytes() == temporary.read_bytes():
                return ()
            raise SettingsRecoveryExhaustedError(
                f"{backup_path.name} already holds different settings content; move or delete it, then retry"
            ) from error
        return (f"settings.json was corrupt; backed up to {backup_path.name}",)
    except OSError as error:
        raise SettingsRecoveryExhaustedError(_SETTINGS_RECOVERY_ERROR) from error
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)


_TERMINAL_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f]")


def _has_terminal_control(value: str) -> bool:
    return _TERMINAL_CONTROL.search(value) is not None


def _validate_model_settings(data: dict) -> None:
    for key in ("last_model", "gpt_fast_model", "gpt_medium_model", "gpt_subagent_model"):
        value = data.get(key)
        if isinstance(value, str) and _has_terminal_control(value):
            raise ValueError(f"settings field {key!r} contains a terminal control character")
    model_settings = data.get("model_settings")
    if isinstance(model_settings, dict):
        for model_id, model in model_settings.items():
            if isinstance(model_id, str) and _has_terminal_control(model_id):
                raise ValueError("model settings contain a terminal control character")
            if isinstance(model, dict):
                for value in model.values():
                    if isinstance(value, str) and _has_terminal_control(value):
                        raise ValueError("model settings contain a terminal control character")


def _normalise_settings(data: dict) -> dict:
    if not isinstance(data.get("category", "All"), str) or data.get("category") not in CATEGORIES:
        data.pop("category", None)
    if not isinstance(data.get("skip_permissions", False), bool):
        data.pop("skip_permissions", None)
    last_model = data.get("last_model")
    if not isinstance(last_model, str) or _has_terminal_control(last_model):
        data.pop("last_model", None)
    for key in ("gpt_fast_model", "gpt_medium_model", "gpt_subagent_model"):
        if key in data and (
            not isinstance(data[key], str) or _has_terminal_control(data[key])
        ):
            data.pop(key)
    if "model_settings" in data and not isinstance(data["model_settings"], dict):
        data.pop("model_settings")
    elif isinstance(data.get("model_settings"), dict):
        normalised_models = {}
        for model_id, model in data["model_settings"].items():
            if not isinstance(model_id, str) or _has_terminal_control(model_id):
                continue
            if not isinstance(model, dict):
                continue
            normalised_model = {
                key: value
                for key, value in model.items()
                if not isinstance(value, str) or not _has_terminal_control(value)
            }
            context_tokens = normalised_model.get("context_tokens")
            if (
                not isinstance(context_tokens, int)
                or isinstance(context_tokens, bool)
                or context_tokens <= 0
            ):
                normalised_model.pop("context_tokens", None)
            if not isinstance(normalised_model.get("auto_compact"), bool):
                normalised_model.pop("auto_compact", None)
            normalised_models[model_id] = normalised_model
        data["model_settings"] = normalised_models
    return data


def _load_settings_unlocked() -> tuple[dict, tuple[str, ...]]:
    global _SETTINGS_DIGEST
    try:
        if not SETTINGS_FILE.exists() and SETTINGS_EXAMPLE_FILE.is_file():
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            SETTINGS_FILE.write_bytes(SETTINGS_EXAMPLE_FILE.read_bytes())
        raw = _read_settings_source()
        text = _decode_settings_source(raw)
        try:
            data = json.loads(text)
        except (RecursionError, ValueError) as error:
            raise _SettingsReadError(str(error), raw) from error
        _SETTINGS_DIGEST = _digest(raw)
        if not isinstance(data, dict):
            return {}, _backup_corrupt_settings(raw)
        return _normalise_settings(data), ()
    except _SettingsOversizedError:
        _SETTINGS_DIGEST = _NO_SETTINGS_DIGEST
        return {}, (_SETTINGS_OVERSIZED_WARNING,)
    except _SettingsReadError as error:
        _SETTINGS_DIGEST = _digest(error.raw)
        return {}, _backup_corrupt_settings(error.raw)
    except OSError:
        _SETTINGS_DIGEST = _NO_SETTINGS_DIGEST
        return {}, (_SETTINGS_UNREADABLE_WARNING,)


def _load_settings() -> tuple[dict, tuple[str, ...]]:
    with _settings_transaction():
        return _load_settings_unlocked()


def _unsaved_notice() -> str:
    return f"not saved: {_SETTINGS_SAVE_CONFLICT_ERROR}"


def _observed_settings_digest() -> str:
    try:
        return _digest(_read_settings_source())
    except (OSError, _SettingsOversizedError):
        return _NO_SETTINGS_DIGEST if not SETTINGS_FILE.exists() else _UNREADABLE_SETTINGS_DIGEST


def _settings_write_is_refused() -> bool:
    return _observed_settings_digest() != _SETTINGS_DIGEST


def _save_settings_unlocked(data: dict) -> None:
    global _SETTINGS_DIGEST
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    if _settings_write_is_refused():
        raise SettingsSaveConflictError(_SETTINGS_SAVE_CONFLICT_ERROR)

    _validate_model_settings(data)
    serialised = json.dumps(data, indent=2) + "\n"
    serialised_bytes = serialised.encode("utf-8")
    if len(serialised_bytes) > _MAX_SETTINGS_BYTES:
        raise ValueError("settings.json is too large")
    temporary_file = tempfile.NamedTemporaryFile(
        mode="wb",
        dir=SETTINGS_FILE.parent,
        prefix=f".{SETTINGS_FILE.name}.",
        suffix=".tmp",
        delete=False,
    )
    temporary = Path(temporary_file.name)
    try:
        with temporary_file:
            temporary_file.write(serialised_bytes)
        temporary.replace(SETTINGS_FILE)
    finally:
        temporary.unlink(missing_ok=True)
    _SETTINGS_DIGEST = _digest(serialised_bytes)


def _update_settings(mutate: Callable[[dict], None]) -> tuple[str, ...]:
    with _settings_transaction():
        settings, warnings = _load_settings_unlocked()
        mutate(settings)
        _save_settings_unlocked(settings)
        return warnings


def _prompt_context_tokens(current: int | None) -> int | None:
    current_text = str(current) if current is not None else "default"
    while True:
        raw = input(f"Context size in tokens [{current_text}]: ").strip().lower()
        if not raw:
            return current
        if raw in {"clear", "default"}:
            return None
        try:
            value = int(raw.replace("_", "").replace(",", ""))
        except ValueError:
            value = 0
        if value > 0:
            return value
        print("Enter a positive integer, or 'clear' to use Claude Code's default.")


def _prompt_auto_compact(current: bool | None) -> bool | None:
    current_text = {True: "on", False: "off", None: "default"}[current]
    while True:
        raw = input(f"Auto-compact: on/off/default [{current_text}]: ").strip().lower()
        if not raw:
            return current
        if raw in {"on", "yes", "y", "true", "1"}:
            return True
        if raw in {"off", "no", "n", "false", "0"}:
            return False
        if raw in {"clear", "default"}:
            return None
        print("Enter 'on', 'off', or 'default'.")


def configure_model_parameters(model_id: str) -> None:
    """Edit the selected model's supported settings without touching other keys."""
    settings, warnings = _load_settings()
    for warning in warnings:
        print(f"  {warning}")
    if _settings_write_is_refused():
        print(f"  {_unsaved_notice()}")
        return
    stored = settings.get("model_settings", {}).get(model_id, {})
    displayed = {field: stored.get(field) for field in _MODEL_FIELDS}

    print(f"Model parameters\n\n  {model_id}\n")
    print("Press Enter to keep a value; type 'clear' to restore its default.\n")
    try:
        chosen = {
            "context_tokens": _prompt_context_tokens(displayed["context_tokens"]),
            "auto_compact": _prompt_auto_compact(displayed["auto_compact"]),
        }
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        return

    changed = {field: value for field, value in chosen.items() if value != displayed[field]}
    observed: dict = {}

    def update(current: dict) -> None:
        current_models = current.setdefault("model_settings", {})
        current_model = dict(current_models.get(model_id, {}))
        for field, value in changed.items():
            if value is None:
                current_model.pop(field, None)
            else:
                current_model[field] = value
        for field in _MODEL_FIELDS:
            if field not in changed:
                observed[field] = current_model.get(field)
        if current_model:
            current_models[model_id] = current_model
        else:
            current_models.pop(model_id, None)

    if not changed:
        update(_load_settings()[0])
    else:
        try:
            save_warnings = _update_settings(update)
        except (SettingsSaveConflictError, SettingsRecoveryExhaustedError, ValueError) as failure:
            print(f"  not saved: {failure}")
            return
        for warning in save_warnings:
            print(f"  {warning}")
        print("\nSaved to data/settings.json.")
    for field, value in observed.items():
        if value != displayed[field]:
            print(f"  {_SETTINGS_OUTSIDE_EDIT.format(field=field)}")
    try:
        input("Press Enter to return to Claudex...")
    except (EOFError, KeyboardInterrupt):
        return


def set_extra_model(key: str, value: str | None) -> None:
    def update(settings: dict) -> None:
        if value is None:
            settings.pop(key, None)
        else:
            settings[key] = value

    _update_settings(update)


def run_picker(models: list[Model], picker_title: str = "Claudex", sub_picker: bool = False) -> PickerResult:
    settings, opening_warnings = _load_settings()
    shown_warnings: list[str] = []

    def note(message: str) -> None:
        if message not in shown_warnings:
            shown_warnings.append(message)

    for warning in opening_warnings:
        note(warning)

    persisted = {key: settings.get(key, _PICKER_DEFAULTS[key]) for key in _PICKER_FIELDS}
    category = persisted["category"]
    skip_permissions = persisted["skip_permissions"]
    last_model = persisted["last_model"]
    gpt_fast_model = settings.get("gpt_fast_model")
    gpt_medium_model = settings.get("gpt_medium_model")
    gpt_subagent_model = settings.get("gpt_subagent_model")
    selected_index = 0
    visible_models: list[Model] = []

    application: Application[PickerResult] | None = None

    gw_status = gw_checked = False
    if not sub_picker:
        from .router_starter import router_is_ready

        def _probe_gateway() -> None:
            nonlocal gw_status, gw_checked
            gw_status, gw_checked = router_is_ready(), True
            if application is not None:
                application.invalidate()

        threading.Thread(target=_probe_gateway, daemon=True).start()

    def persist(model: Model | None = None) -> None:
        if sub_picker:
            return
        nonlocal last_model
        if model is not None:
            last_model = model.id
        shown = {"category": category, "skip_permissions": skip_permissions, "last_model": last_model}
        changed = {key: value for key, value in shown.items() if value != persisted[key]}
        if not changed:
            return
        observed = {}

        def update(current: dict) -> None:
            current.update(changed)
            for key in _PICKER_FIELDS:
                if key not in changed:
                    observed[key] = current.get(key, _PICKER_DEFAULTS[key])

        try:
            save_warnings = _update_settings(update)
        except SettingsSaveConflictError:
            note(_unsaved_notice())
            return
        except (SettingsRecoveryExhaustedError, ValueError) as failure:
            note(f"not saved: {failure}")
            return
        persisted.update(changed)
        if not save_warnings:
            for key, value in observed.items():
                if value != persisted[key]:
                    note(_SETTINGS_OUTSIDE_EDIT.format(field=key))
        for warning in save_warnings:
            note(warning)

    def _result(action: str, model: Model | None = None, context_tokens: int | None = None,
                auto_compact: bool | None = None) -> PickerResult:
        current_settings, _ = _load_settings()
        return PickerResult(
            action,
            model,
            skip_permissions,
            context_tokens,
            auto_compact,
            current_settings.get("gpt_fast_model"),
            current_settings.get("gpt_medium_model"),
            current_settings.get("gpt_subagent_model"),
        )

    def selected_model() -> Model | None:
        if not visible_models:
            return None
        return visible_models[selected_index]

    def refresh_visible(preferred_id: str | None = None) -> None:
        nonlocal visible_models, selected_index

        previous = preferred_id
        if previous is None:
            current = selected_model()
            previous = current.id if current else last_model

        visible_models = filter_models(models, category, search_buffer.text)
        selected_index = 0

        if previous:
            for index, model in enumerate(visible_models):
                if model.id == previous:
                    selected_index = index
                    break

        if application is not None:
            application.invalidate()

    def on_search_changed(_: Buffer) -> None:
        refresh_visible()

    search_buffer = Buffer(multiline=False, on_text_changed=on_search_changed)

    def header_text():
        lines = [
            [
                ("class:title", f" {picker_title} "),
                ("class:muted", "  live models from CLIProxyAPI + local pool aliases"),
            ],
            [
                ("class:label", " Filter: "),
                ("class:accent", category),
                ("class:label", "   Models: "),
                ("class:accent", str(len(visible_models))),
                ("class:label", "   Dangerous: "),
                (
                    "class:danger" if skip_permissions else "class:safe",
                    "ON" if skip_permissions else "OFF",
                ),
            ],
        ]
        lines.extend([[("class:danger", f"{warning}  ")] for warning in shown_warnings])
        if not sub_picker:
            roles = [
                fragment
                for label, value in (
                    ("Fast", gpt_fast_model),
                    ("Medium", gpt_medium_model),
                    ("Subagent", gpt_subagent_model),
                )
                for fragment in (
                    ("class:label", f" {label}: "),
                    ("class:accent" if value else "class:muted", value or "default"),
                )
            ]
            roles.append(("class:label", "   GW: "))
            if gw_checked:
                roles.append(("class:safe" if gw_status else "class:danger",
                              "\u25cf" if gw_status else "\u25cb"))
            else:
                roles.append(("class:muted", "\u25cb\u2026 checking"))
            lines.append(roles)
        fragments: list[tuple[str, str]] = []
        for line in lines:
            if fragments:
                fragments.append(("", "\n"))
            fragments.extend(line)
        return fragments

    def footer_text():
        fragments = []
        for key, label in _SUB_PICKER_FOOTER if sub_picker else _LAUNCHER_FOOTER:
            fragments.append(("class:key", f" {key} "))
            fragments.append(("class:muted", f"{label}  "))
        return fragments

    def model_text():
        if not visible_models:
            return [("class:muted", "  No matching models.")]

        fragments = []
        for index, model in enumerate(visible_models):
            is_selected = index == selected_index
            prefix = "  › " if is_selected else "    "
            style = "class:selected" if is_selected else "class:model"
            fragments.append((style, prefix + model.id))
            if model.owner:
                fragments.append(("class:owner", f"  [{model.owner}]"))
            if index < len(visible_models) - 1:
                fragments.append(("", "\n"))
        return fragments

    def cursor_position() -> Point:
        return Point(x=0, y=selected_index)

    header = Window(FormattedTextControl(header_text), wrap_lines=True, dont_extend_height=True, style="class:header")

    search_control = BufferControl(buffer=search_buffer)
    search_row = VSplit(
        [
            Window(
                FormattedTextControl([("class:label", " Search: ")]),
                width=9,
                height=1,
            ),
            Window(
                search_control,
                height=1,
                style="class:search",
            ),
        ],
        height=1,
    )

    model_control = FormattedTextControl(model_text, get_cursor_position=cursor_position, focusable=False)
    model_window = Window(model_control, wrap_lines=False, always_hide_cursor=True, right_margins=[])

    footer = Window(FormattedTextControl(footer_text), wrap_lines=True, dont_extend_height=True, style="class:footer")

    root = HSplit(
        [
            header,
            Window(height=1, char="─", style="class:border"),
            search_row,
            Window(height=1, char="─", style="class:border"),
            model_window,
            Window(height=1, char="─", style="class:border"),
            footer,
        ]
    )

    bindings = KeyBindings()

    def _move(event, delta: int) -> None:
        nonlocal selected_index
        if visible_models:
            selected_index = min(len(visible_models) - 1, max(0, selected_index + delta))
            event.app.invalidate()

    for key, delta in (("up", -1), ("down", 1), ("pageup", -10), ("pagedown", 10)):
        bindings.add(key)(lambda event, _delta=delta: _move(event, _delta))

    @bindings.add("home")
    def _home(event) -> None:
        _move(event, -(1 << 60))

    @bindings.add("end")
    def _end(event) -> None:
        _move(event, 1 << 60)

    def _shift_category(event, step: int) -> None:
        nonlocal category
        current = selected_model()
        category = CATEGORIES[(CATEGORIES.index(category) + step) % len(CATEGORIES)]
        refresh_visible(current.id if current else None)
        event.app.invalidate()

    bindings.add("tab")(lambda event: _shift_category(event, 1))
    bindings.add("s-tab")(lambda event: _shift_category(event, -1))

    if not sub_picker:
        @bindings.add("c-s")
        def _toggle_permissions(event) -> None:
            nonlocal skip_permissions
            skip_permissions = not skip_permissions
            persist(selected_model())
            event.app.invalidate()

        @bindings.add("f5")
        @bindings.add("c-r")
        def _refresh(event) -> None:
            persist(selected_model())
            _exit_once(event, _result("refresh"))

        for key, action in (("f6", "configure"), ("f7", "pools"), ("f8", "management"), ("f9", "gateway")):
            @bindings.add(key)
            def _function_key(event, _action: str = action) -> None:
                persist(selected_model())
                _exit_once(event, _result(_action))

        @bindings.add("f10")
        def _model_parameters(event) -> None:
            model = selected_model()
            if model is not None:
                persist(model)
                _exit_once(event, _result("model_parameters", model))

    @bindings.add("delete")
    def _clear_selection(event) -> None:
        if search_buffer.text:
            search_buffer.text = ""
        elif sub_picker:
            _exit_once(event, PickerResult("clear", None, skip_permissions, None))

    @bindings.add("enter")
    def _launch(event) -> None:
        model = selected_model()
        if model:
            persist(model)
            _exit_once(event, _result("launch", model, get_model_context(model.id), get_model_autocompact(model.id)))

    @bindings.add("escape")
    def _escape(event) -> None:
        if search_buffer.text:
            search_buffer.text = ""
        else:
            persist(selected_model())
            _exit_once(event, _result("cancel" if sub_picker else "exit"))

    @bindings.add("c-c")
    @bindings.add("c-d")
    def _interrupt(event) -> None:
        _exit_once(event, _result("cancel" if sub_picker else "exit"))

    style = Style.from_dict(
        {
            "": "bg:#111318 #d7dae0",
            "header": "bg:#181b22",
            "footer": "bg:#181b22",
            "title": "bold #8ec7ff",
            "label": "#aeb6c2",
            "accent": "bold #8ec7ff",
            "danger": "bold #ff6b6b",
            "safe": "bold #7bd88f",
            "search": "bg:#20242d #ffffff",
            "border": "#3a414d",
            "model": "#d7dae0",
            "selected": "bold reverse",
            "owner": "#7f8998",
            "muted": "#7f8998",
            "key": "bold #8ec7ff",
        }
    )

    refresh_visible(last_model)

    application = Application(
        layout=Layout(root, focused_element=search_control),
        key_bindings=bindings,
        style=style,
        full_screen=True,
        mouse_support=False,
        erase_when_done=True,
        min_redraw_interval=0.03,
    )

    return application.run()
