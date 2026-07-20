from __future__ import annotations

import json
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

def get_model_autocompact(model_id: str) -> bool | None:
    settings = _load_settings()
    return settings.get('model_settings', {}).get(model_id, {}).get('auto_compact')

def get_model_context(
    model_id: str,
) -> int | None:
    settings = _load_settings()

    model_settings = settings.get(
        "model_settings",
        {},
    )

    model = model_settings.get(
        model_id,
        {},
    )
    return model.get("context_tokens")

def _load_settings() -> dict:
    if not SETTINGS_FILE.exists() and SETTINGS_EXAMPLE_FILE.is_file():
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_bytes(SETTINGS_EXAMPLE_FILE.read_bytes())
    try:
        with SETTINGS_FILE.open("r", encoding="utf-8") as file:
            data = json.load(file)
            return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        # If the user made a syntax error manually editing, don't silently wipe it!
        # Back it up and return an empty dict so they don't lose their file completely.
        import time
        if SETTINGS_FILE.exists():
            backup_path = SETTINGS_FILE.with_suffix(f".broken_{int(time.time())}.json")
            SETTINGS_FILE.rename(backup_path)
            print(f"\nWARNING: settings.json had a syntax error! Backed up to {backup_path.name}")
        return {}
    except (FileNotFoundError, OSError):
        return {}


def _save_settings(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary = SETTINGS_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    temporary.replace(SETTINGS_FILE)

def set_extra_model(key: str, value: str | None) -> None:
    settings = _load_settings()
    if value is None:
        settings.pop(key, None)
    else:
        settings[key] = value
    _save_settings(settings)

def configure_extra_models() -> None:
    settings = _load_settings()
    print("\n--- Configure Extra Models for GPT ---")
    print("Leave blank to keep current, type 'clear' to reset to defaults.")
    
    current_fast = settings.get("gpt_fast_model", "")
    new_fast = input(f"Fast/Haiku model ID [{current_fast}]: ").strip()
    if new_fast.lower() == "clear":
        settings.pop("gpt_fast_model", None)
    elif new_fast:
        settings["gpt_fast_model"] = new_fast
        
    current_med = settings.get("gpt_medium_model", "")
    new_med = input(f"Medium/Sonnet/Subagent model ID [{current_med}]: ").strip()
    if new_med.lower() == "clear":
        settings.pop("gpt_medium_model", None)
    elif new_med:
        settings["gpt_medium_model"] = new_med
        
    _save_settings(settings)


def run_picker(models: list[Model], picker_title: str = "Claudex", sub_picker: bool = False) -> PickerResult:
    settings = _load_settings()
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

    # Cache router status once — never do network I/O in render functions.
    gw_status = False
    if not sub_picker:
        from .router_starter import router_is_ready
        gw_status = router_is_ready()

    application: Application[PickerResult] | None = None

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
                ("class:label", "   GW: "),
                ("class:safe" if gw_status else "class:danger", "\u25cf" if gw_status else "\u25cb"),
            ])
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
                ("class:key", "Del "),
                ("class:muted", "clear  "),
                ("class:key", "Ctrl+S "),
                ("class:muted", "permissions  "),
                ("class:key", "Esc "),
                ("class:muted", "clear  "),
                ("class:key", "Ctrl+Q "),
                ("class:muted", "exit"),
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

    @bindings.add("up")
    def _up(event) -> None:
        nonlocal selected_index
        if visible_models:
            selected_index = max(0, selected_index - 1)
            event.app.invalidate()

    @bindings.add("down")
    def _down(event) -> None:
        nonlocal selected_index
        if visible_models:
            selected_index = min(len(visible_models) - 1, selected_index + 1)
            event.app.invalidate()

    @bindings.add("pageup")
    def _page_up(event) -> None:
        nonlocal selected_index
        selected_index = max(0, selected_index - 10)
        event.app.invalidate()

    @bindings.add("pagedown")
    def _page_down(event) -> None:
        nonlocal selected_index
        if visible_models:
            selected_index = min(len(visible_models) - 1, selected_index + 10)
            event.app.invalidate()

    @bindings.add("home")
    def _home(event) -> None:
        nonlocal selected_index
        selected_index = 0
        event.app.invalidate()

    @bindings.add("end")
    def _end(event) -> None:
        nonlocal selected_index
        if visible_models:
            selected_index = len(visible_models) - 1
            event.app.invalidate()

    @bindings.add("tab")
    def _next_category(event) -> None:
        nonlocal category
        current_id = selected_model().id if selected_model() else None
        category = CATEGORIES[(CATEGORIES.index(category) + 1) % len(CATEGORIES)]
        refresh_visible(current_id)
        event.app.invalidate()

    @bindings.add("s-tab")
    def _previous_category(event) -> None:
        nonlocal category
        current_id = selected_model().id if selected_model() else None
        category = CATEGORIES[(CATEGORIES.index(category) - 1) % len(CATEGORIES)]
        refresh_visible(current_id)
        event.app.invalidate()

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
        event.app.exit(PickerResult("refresh", None, skip_permissions, None, None, gpt_fast_model, gpt_medium_model, gpt_subagent_model))

    @bindings.add("f6")
    def _config(event) -> None:
        if sub_picker:
            return
        persist(selected_model())
        event.app.exit(PickerResult("configure", None, skip_permissions, None, None, gpt_fast_model, gpt_medium_model, gpt_subagent_model))

    @bindings.add("f7")
    def _pools(event) -> None:
        if sub_picker:
            return
        persist(selected_model())
        event.app.exit(PickerResult("pools", None, skip_permissions, None, None, gpt_fast_model, gpt_medium_model, gpt_subagent_model))

    @bindings.add("f8")
    def _management(event) -> None:
        if sub_picker:
            return
        persist(selected_model())
        event.app.exit(PickerResult("management", None, skip_permissions, None, None, gpt_fast_model, gpt_medium_model, gpt_subagent_model))

    @bindings.add("f9")
    def _gateway(event) -> None:
        if sub_picker:
            return
        persist(selected_model())
        event.app.exit(PickerResult("gateway", None, skip_permissions, None, None, gpt_fast_model, gpt_medium_model, gpt_subagent_model))

    @bindings.add("delete")
    def _clear_selection(event) -> None:
        if sub_picker:
            event.app.exit(PickerResult("clear", None, skip_permissions, None, None, None, None, None))

    @bindings.add("enter")
    def _launch(event) -> None:
        model = selected_model()
        if model:
            persist(model)
            event.app.exit(PickerResult("launch", model, skip_permissions, get_model_context(model.id), get_model_autocompact(model.id), gpt_fast_model, gpt_medium_model, gpt_subagent_model))

    @bindings.add("escape")
    def _escape(event) -> None:
        if search_buffer.text:
            search_buffer.text = ""
        else:
            persist(selected_model())
            action = "cancel" if sub_picker else "exit"
            event.app.exit(PickerResult(action, None, skip_permissions, None, None, gpt_fast_model, gpt_medium_model, gpt_subagent_model))

    @bindings.add("c-q")
    def _quit(event) -> None:
        persist(selected_model())
        action = "quit" if sub_picker else "exit"
        event.app.exit(PickerResult(action, None, skip_permissions, None, None, gpt_fast_model, gpt_medium_model, gpt_subagent_model))

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
