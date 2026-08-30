from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass

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
    """Ignore a queued key event after Prompt Toolkit has accepted a result."""
    if not event.app.is_done:
        event.app.exit(result)


def _model_settings_for(model_id: str) -> dict:
    settings = _load_settings()
    model_settings = settings.get("model_settings", {})
    if not isinstance(model_settings, dict):
        return {}
    model = model_settings.get(model_id, {})
    return model if isinstance(model, dict) else {}


def get_model_autocompact(model_id: str) -> bool | None:
    value = _model_settings_for(model_id).get("auto_compact")
    return value if isinstance(value, bool) else None


def get_model_context(model_id: str) -> int | None:
    value = _model_settings_for(model_id).get("context_tokens")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None

_SETTINGS_DIGEST: str | None = None
_SETTINGS_WARNINGS: list[str] = []


def _drain_settings_warnings() -> list[str]:
    warnings = _SETTINGS_WARNINGS[:]
    _SETTINGS_WARNINGS.clear()
    return warnings


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _load_settings() -> dict:
    global _SETTINGS_DIGEST
    try:
        if not SETTINGS_FILE.exists() and SETTINGS_EXAMPLE_FILE.is_file():
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            SETTINGS_FILE.write_bytes(SETTINGS_EXAMPLE_FILE.read_bytes())
        raw = SETTINGS_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
        _SETTINGS_DIGEST = _digest(raw)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        _SETTINGS_DIGEST = None
        if SETTINGS_FILE.exists():
            backup_path = SETTINGS_FILE.with_suffix(f".broken_{int(time.time())}.json")
            os.replace(SETTINGS_FILE, backup_path)
            _SETTINGS_WARNINGS.append(f"settings.json was corrupt; backed up to {backup_path.name}")
        return {}
    except OSError:
        _SETTINGS_DIGEST = None
        return {}


def _save_settings(data: dict) -> None:
    global _SETTINGS_DIGEST
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if _SETTINGS_DIGEST is not None:
        try:
            actual = _digest(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            actual = ""
        if actual != _SETTINGS_DIGEST:
            raise RuntimeError("settings.json changed on disk; reload before saving so no edits are lost.")
    temporary = SETTINGS_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    temporary.replace(SETTINGS_FILE)
    _SETTINGS_DIGEST = _digest(json.dumps(data, indent=2) + "\n")


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
    settings = _load_settings()
    for warning in _drain_settings_warnings():
        print(f"  {warning}")
    all_models = settings.get("model_settings")
    if not isinstance(all_models, dict):
        all_models = {}
        settings["model_settings"] = all_models

    existing = all_models.get(model_id)
    model = dict(existing) if isinstance(existing, dict) else {}
    current_context = model.get("context_tokens")
    if (
        not isinstance(current_context, int)
        or isinstance(current_context, bool)
        or current_context <= 0
    ):
        current_context = None
    current_auto_compact = model.get("auto_compact")
    if not isinstance(current_auto_compact, bool):
        current_auto_compact = None

    print(f"Model parameters\n\n  {model_id}\n")
    print("Press Enter to keep a value; type 'clear' to restore its default.\n")
    context_tokens = _prompt_context_tokens(current_context)
    auto_compact = _prompt_auto_compact(current_auto_compact)

    if context_tokens is None:
        model.pop("context_tokens", None)
    else:
        model["context_tokens"] = context_tokens
    if auto_compact is None:
        model.pop("auto_compact", None)
    else:
        model["auto_compact"] = auto_compact

    if model:
        all_models[model_id] = model
    else:
        all_models.pop(model_id, None)
    _save_settings(settings)
    print("\nSaved to data/settings.json.")
    input("Press Enter to return to Claudex...")


def set_extra_model(key: str, value: str | None) -> None:
    settings = _load_settings()
    if value is None:
        settings.pop(key, None)
    else:
        settings[key] = value
    _save_settings(settings)


def run_picker(models: list[Model], picker_title: str = "Claudex", sub_picker: bool = False) -> PickerResult:
    settings = _load_settings()
    load_warnings = _drain_settings_warnings()
    category = str(settings.get("category", "All"))
    if category not in CATEGORIES:
        category = "All"

    skip_permissions = bool(settings.get("skip_permissions", False))
    last_model = str(settings.get("last_model", ""))
    gpt_fast_model = settings.get("gpt_fast_model")
    gpt_medium_model = settings.get("gpt_medium_model")
    gpt_subagent_model = settings.get("gpt_subagent_model")
    selected_index = 0
    visible_models: list[Model] = []

    application: Application[PickerResult] | None = None

    gw_status, gw_checked = False, sub_picker
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
        current = _load_settings()
        current["category"] = category
        current["skip_permissions"] = skip_permissions
        current["last_model"] = last_model
        _save_settings(current)

    def _result(action: str, model: Model | None = None, context_tokens: int | None = None,
                auto_compact: bool | None = None) -> PickerResult:
        return PickerResult(action, model, skip_permissions, context_tokens, auto_compact,
                            gpt_fast_model, gpt_medium_model, gpt_subagent_model)

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
        parts = [
            ("class:title", f" {picker_title} "),
            ("class:muted", "  live models from CLIProxyAPI + local pool aliases\n"),
            ("class:label", " Filter: "),
            ("class:accent", category),
            ("class:label", "   Models: "),
            ("class:accent", str(len(visible_models))),
            ("class:label", "   Dangerous: "),
            (
                "class:danger" if skip_permissions else "class:safe",
                "ON" if skip_permissions else "OFF",
            ),
        ]
        if not sub_picker:
            parts.append(("", "\n"))
            if load_warnings:
                for warning in load_warnings:
                    parts.append(("class:danger", f"{warning}  "))
            fast_style = "class:accent" if gpt_fast_model else "class:muted"
            med_style = "class:accent" if gpt_medium_model else "class:muted"
            sub_style = "class:accent" if gpt_subagent_model else "class:muted"
            parts.extend([
                ("class:label", " Fast: "),
                (fast_style, gpt_fast_model or "default"),
                ("class:label", "   Medium: "),
                (med_style, gpt_medium_model or "default"),
                ("class:label", "   Subagent: "),
                (sub_style, gpt_subagent_model or "default"),
            ])
            parts.append(("class:label", "   GW: "))
            if gw_checked:
                parts.append(("class:safe" if gw_status else "class:danger",
                              "\u25cf" if gw_status else "\u25cb"))
            else:
                parts.append(("class:muted", "\u25cb\u2026 checking"))
        return parts

    def model_text():
        if not visible_models:
            return [("class:muted", "\n  No matching models.")]

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

    header = Window(
        FormattedTextControl(header_text),
        height=4 if not sub_picker else 3,
        style="class:header",
    )

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

    model_control = FormattedTextControl(
        model_text,
        get_cursor_position=cursor_position,
        focusable=False,
    )
    model_window = Window(
        model_control,
        wrap_lines=False,
        always_hide_cursor=True,
        right_margins=[],
    )

    footer = Window(
        FormattedTextControl(
            [
                ("class:key", " ↑↓ "),
                ("class:muted", "select  "),
                ("class:key", "Enter "),
                ("class:muted", "launch  "),
                ("class:key", "Tab "),
                ("class:muted", "category  "),
                ("class:key", "F5 "),
                ("class:muted", "refresh  "),
                ("class:key", "F6 "),
                ("class:muted", "model roles  "),
                ("class:key", "F7 "),
                ("class:muted", "pools  "),
                ("class:key", "F8 "),
                ("class:muted", "CLIProxy UI  "),
                ("class:key", "F9 "),
                ("class:muted", "gateway  "),
                ("class:key", "F10 "),
                ("class:muted", "model params  "),
                ("class:key", "Del "),
                ("class:muted", "clear  "),
                ("class:key", "Ctrl+S "),
                ("class:muted", "permissions  "),
                ("class:key", "Esc "),
                ("class:muted", "clear/exit"),
            ]
        ),
        height=1,
        style="class:footer",
    )

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
            if sub_picker:
                return
            persist(selected_model())
            _exit_once(event, _result(_action))

    @bindings.add("f10")
    def _model_parameters(event) -> None:
        if sub_picker:
            return
        model = selected_model()
        if model is not None:
            persist(model)
            _exit_once(event, _result("model_parameters", model))

    @bindings.add("delete")
    def _clear_selection(event) -> None:
        if sub_picker:
            _exit_once(event, PickerResult("clear", None, skip_permissions, None, None, None, None, None))
        elif search_buffer.text:
            search_buffer.text = ""
            event.app.invalidate()

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
