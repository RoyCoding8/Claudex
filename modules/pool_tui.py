from __future__ import annotations

import os
from dataclasses import dataclass, replace

from prompt_toolkit import Application
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style

from .models import Model
from .pools import (
    _MAX_INTEGER,
    POOLS_FILE,
    STRATEGIES,
    ModelPool,
    PoolMember,
    PoolSaveConflictError,
    PoolSaveRecoveryError,
    PoolSaveUnreadableError,
    _release_conflict_path,
    adopt_current_pools_digest,
    conflict_artifact_carries_edits,
    conflict_artifact_holds,
    ensure_default_pools_file,
    load_pools,
    reservation_digest,
    reserve_pools_conflict_path,
    save_pools,
)

_MAX_CONFLICT_RECOVERY_ATTEMPTS = 8


class _Cancelled:
    __slots__ = ()


@dataclass(frozen=True, slots=True)
class _Answered:
    value: int | None


_OptionalInt = _Answered | _Cancelled


_STYLE = Style.from_dict(
    {
        "": "bg:#111318 #d7dae0",
        "header": "bg:#181b22",
        "footer": "bg:#181b22",
        "title": "bold #8ec7ff",
        "label": "#aeb6c2",
        "accent": "bold #8ec7ff",
        "danger": "bold #ff6b6b",
        "safe": "bold #7bd88f",
        "border": "#3a414d",
        "selected": "bold reverse",
        "item": "#d7dae0",
        "muted": "#7f8998",
        "key": "bold #8ec7ff",
        "enabled": "#7bd88f",
        "disabled": "#555a66",
        "warn": "bold #ffb347",
    }
)


def _clear() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def _prompt_text(label: str, default: str = "") -> str | None:
    """Simple line prompt.  Returns *None* when the user leaves it blank
    and no default is set, or on Ctrl-C / Ctrl-D."""
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"  {label}{suffix}: ").strip()
    except (KeyboardInterrupt, EOFError):
        return None
    return value or default or None


def _prompt_int(
    label: str,
    default: int | None = None,
    *,
    minimum: int = 1,
) -> _OptionalInt:
    suffix = f" [{default}]" if default is not None else ""
    while True:
        try:
            raw = input(f"  {label} (Enter to skip){suffix}: ").strip()
        except (KeyboardInterrupt, EOFError):
            return _Cancelled()
        if not raw:
            return _Answered(default)
        try:
            number = int(raw)
        except ValueError:
            print("  Enter a whole number.")
            continue
        if number < minimum:
            print(f"  Must be at least {minimum}.")
            continue
        if number > _MAX_INTEGER:
            print(f"  Must be at most {_MAX_INTEGER}.")
            continue
        return _Answered(number)


def _prompt_strategy(default: str = STRATEGIES[0]) -> str | None:
    suffix = f" [{default}]" if default else ""
    while True:
        try:
            raw = input(f"  Strategy ({' / '.join(STRATEGIES)}){suffix}: ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            return None
        if not raw:
            return default
        if raw in STRATEGIES:
            return raw
        print(f"  Enter one of: {', '.join(STRATEGIES)}.")


@dataclass
class _PoolAction:
    kind: str
    index: int = -1


def _pool_list_tui(pools: list[ModelPool]) -> _PoolAction:
    """Full-screen pool list.  Returns the action the user chose."""
    selected = 0
    confirming = False

    @Condition
    def normal() -> bool:
        return not confirming

    @Condition
    def deleting() -> bool:
        return confirming

    def header_text():
        return [
            ("class:title", " Claudex Pool Manager "),
            ("class:muted", "  load-balanced model routing\n"),
            ("class:label", " Pools: "),
            ("class:accent", str(len(pools))),
            ("class:muted", "   tip: set RPM for smarter rate-aware routing"),
        ]

    def body_text():
        if not pools:
            return [
                ("class:muted", "\n  No pools configured.\n\n"),
                ("class:muted", "  Press "),
                ("class:key", "A"),
                ("class:muted", " to create your first pool.\n"),
            ]
        parts: list[tuple[str, str]] = []
        for i, pool in enumerate(pools):
            sel = i == selected
            prefix = "  › " if sel else "    "
            style = "class:selected" if sel else "class:item"

            icon = "●" if pool.enabled else "○"
            icon_style = "class:enabled" if pool.enabled else "class:disabled"

            rpms = [m.rpm for m in pool.members if m.rpm]
            capacity_str = f"{sum(rpms):,} RPM" if rpms else "auto"
            info = f"  {len(pool.members)} models · {capacity_str}"
            if pool.strategy != STRATEGIES[0]:
                info += f" · {pool.strategy}"
            state = " enabled" if pool.enabled else " disabled"

            parts.append((icon_style if not sel else style, f"{prefix}{icon} "))
            parts.append((style, pool.name))
            parts.append(("class:muted" if not sel else style, info))
            parts.append((icon_style if not sel else style, state))
            if i < len(pools) - 1:
                parts.append(("", "\n"))
        return parts

    def cursor_pos() -> Point:
        return Point(0, selected)

    def footer_text():
        if confirming and pools:
            name = pools[selected].name
            return [
                ("class:danger", f" Delete '{name}'?  "),
                ("class:key", "Y "),
                ("class:muted", "confirm  "),
                ("class:muted", "any other key cancels"),
            ]
        return [
            ("class:key", " ↑↓ "),
            ("class:muted", "select  "),
            ("class:key", "A "),
            ("class:muted", "add  "),
            ("class:key", "Enter "),
            ("class:muted", "edit  "),
            ("class:key", "T "),
            ("class:muted", "toggle  "),
            ("class:key", "D "),
            ("class:muted", "delete  "),
            ("class:key", "Esc "),
            ("class:muted", "back"),
        ]

    header = Window(FormattedTextControl(header_text), height=3, style="class:header")
    body = Window(
        FormattedTextControl(body_text, get_cursor_position=cursor_pos, focusable=False),
        wrap_lines=False,
        always_hide_cursor=True,
        right_margins=[],
    )
    footer = Window(FormattedTextControl(footer_text), height=1, style="class:footer")

    root = HSplit(
        [
            header,
            Window(height=1, char="─", style="class:border"),
            body,
            Window(height=1, char="─", style="class:border"),
            footer,
        ]
    )

    kb = KeyBindings()

    def _move(event, delta: int) -> None:
        nonlocal selected
        if pools:
            selected = min(len(pools) - 1, max(0, selected + delta))
            event.app.invalidate()

    for key, delta in (("up", -1), ("down", 1), ("pageup", -10), ("pagedown", 10)):
        kb.add(key, filter=normal)(lambda event, _delta=delta: _move(event, _delta))

    @kb.add("home", filter=normal)
    def _home(event) -> None:
        _move(event, -(1 << 60))

    @kb.add("end", filter=normal)
    def _end(event) -> None:
        _move(event, 1 << 60)

    @kb.add("a", filter=normal)
    def _add(event) -> None:
        event.app.exit(_PoolAction("add"))

    @kb.add("enter", filter=normal)
    def _edit(event) -> None:
        if pools:
            event.app.exit(_PoolAction("edit", selected))

    @kb.add("t", filter=normal)
    def _toggle(event) -> None:
        if pools:
            event.app.exit(_PoolAction("toggle", selected))

    @kb.add("d", filter=normal)
    def _start_delete(event) -> None:
        nonlocal confirming
        if pools:
            confirming = True
            event.app.invalidate()

    @kb.add("escape", filter=normal)
    @kb.add("q", filter=normal)
    @kb.add("c-c")
    @kb.add("c-d")
    def _back(event) -> None:
        event.app.exit(_PoolAction("back"))

    @kb.add("y", filter=deleting)
    def _confirm_delete(event) -> None:
        event.app.exit(_PoolAction("delete", selected))

    @kb.add("<any>", filter=deleting)
    def _cancel_delete(event) -> None:
        nonlocal confirming
        confirming = False
        event.app.invalidate()

    app: Application = Application(
        layout=Layout(root),
        key_bindings=kb,
        style=_STYLE,
        full_screen=True,
        mouse_support=False,
        erase_when_done=True,
        min_redraw_interval=0.03,
    )
    return app.run()


@dataclass
class _MemberAction:
    kind: str
    index: int = -1
    members: tuple[PoolMember, ...] = ()


def _member_editor_tui(
    pool_name: str,
    members: list[PoolMember],
) -> _MemberAction:
    """Full-screen member list for a single pool."""
    selected = 0
    confirming = False

    @Condition
    def normal() -> bool:
        return not confirming

    @Condition
    def deleting() -> bool:
        return confirming

    def header_text():
        rpms = [m.rpm for m in members if m.rpm]
        mode_str = f"{sum(rpms):,} RPM" if rpms else "auto"
        return [
            ("class:title", f" Pool: {pool_name} "),
            ("class:muted", "  member configuration\n"),
            ("class:label", " Members: "),
            ("class:accent", str(len(members))),
            ("class:label", "   Routing: "),
            ("class:accent", mode_str),
        ]

    def body_text():
        if not members:
            return [
                ("class:muted", "\n  No members yet.\n\n"),
                ("class:muted", "  Press "),
                ("class:key", "A"),
                ("class:muted", " to add a model to this pool.\n"),
            ]
        parts: list[tuple[str, str]] = []
        for i, member in enumerate(members):
            sel = i == selected
            prefix = "  › " if sel else "    "
            style = "class:selected" if sel else "class:item"

            rpm_str = f"{member.rpm:,} RPM" if member.rpm else "auto"
            pri_str = f"  [Priority: {member.priority}]" if member.priority is not None else ""
            detail = f"  {rpm_str}{pri_str}"

            parts.append((style, prefix + member.model))
            parts.append(("class:muted" if not sel else style, detail))
            if i < len(members) - 1:
                parts.append(("", "\n"))
        return parts

    def cursor_pos() -> Point:
        return Point(0, selected)

    def footer_text():
        if confirming and members:
            return [
                ("class:danger", f" Delete '{members[selected].model}'?  "),
                ("class:key", "Y "),
                ("class:muted", "confirm  "),
                ("class:muted", "any other key cancels"),
            ]
        parts: list[tuple[str, str]] = []
        if not members:
            parts.append(("class:warn", " ⚠ Add at least one member to save  "))
        parts.extend(
            [
                ("class:key", " ↑↓ "),
                ("class:muted", "select  "),
                ("class:key", "A "),
                ("class:muted", "add  "),
                ("class:key", "E "),
                ("class:muted", "edit limits  "),
                ("class:key", "D "),
                ("class:muted", "delete  "),
                ("class:key", "Enter "),
                ("class:muted", "save  "),
                ("class:key", "Esc "),
                ("class:muted", "cancel"),
            ]
        )
        return parts

    header = Window(FormattedTextControl(header_text), height=3, style="class:header")
    body = Window(
        FormattedTextControl(body_text, get_cursor_position=cursor_pos, focusable=False),
        wrap_lines=False,
        always_hide_cursor=True,
        right_margins=[],
    )
    footer = Window(FormattedTextControl(footer_text), height=1, style="class:footer")

    root = HSplit(
        [
            header,
            Window(height=1, char="─", style="class:border"),
            body,
            Window(height=1, char="─", style="class:border"),
            footer,
        ]
    )

    kb = KeyBindings()

    def _move(event, delta: int) -> None:
        nonlocal selected
        if members:
            selected = min(len(members) - 1, max(0, selected + delta))
            event.app.invalidate()

    for key, delta in (("up", -1), ("down", 1), ("pageup", -10), ("pagedown", 10)):
        kb.add(key)(lambda event, _delta=delta: _move(event, _delta))

    @kb.add("a", filter=normal)
    def _add(event) -> None:
        event.app.exit(_MemberAction("add"))

    @kb.add("e", filter=normal)
    def _edit(event) -> None:
        if members:
            event.app.exit(_MemberAction("edit", selected))

    @kb.add("d", filter=normal)
    def _start_delete(event) -> None:
        nonlocal confirming
        if members:
            confirming = True
            event.app.invalidate()

    @kb.add("escape", filter=normal)
    @kb.add("c-c")
    @kb.add("c-d")
    def _cancel(event) -> None:
        event.app.exit(_MemberAction("cancel"))

    @kb.add("y", filter=deleting)
    def _confirm_delete(event) -> None:
        nonlocal confirming, selected
        confirming = False
        members.pop(selected)
        if selected >= len(members) and members:
            selected = len(members) - 1
        event.app.invalidate()

    @kb.add("<any>", filter=deleting)
    def _cancel_delete(event) -> None:
        nonlocal confirming
        confirming = False
        event.app.invalidate()

    @kb.add("enter", filter=normal)
    def _save(event) -> None:
        if members:
            event.app.exit(_MemberAction("save", members=tuple(members)))

    app: Application = Application(
        layout=Layout(root),
        key_bindings=kb,
        style=_STYLE,
        full_screen=True,
        mouse_support=False,
        erase_when_done=True,
        min_redraw_interval=0.03,
    )
    return app.run()


def _edit_members_flow(
    pool_name: str,
    members: list[PoolMember],
    upstream_models: list[Model],
) -> tuple[PoolMember, ...] | None:
    """Member-editor loop; returns the final members or *None* if cancelled."""
    while True:
        result = _member_editor_tui(pool_name, members)

        if result.kind == "cancel":
            return None
        if result.kind == "save":
            return result.members

        if result.kind == "add":
            from .tui import run_picker

            pick = run_picker(
                upstream_models,
                picker_title="[Pool member] Select a model · Esc to cancel",
                sub_picker=True,
            )
            if pick.action == "launch" and pick.model:
                if any(m.model == pick.model.id for m in members):
                    _clear()
                    try:
                        input("  That model is already in this pool. Press Enter...")
                    except (KeyboardInterrupt, EOFError):
                        pass
                    continue
                _clear()
                print(f"\n  Adding: {pick.model.id}\n")
                rpm = _prompt_int("Requests per minute (RPM)")
                if isinstance(rpm, _Cancelled):
                    continue
                priority = _prompt_int(
                    "Priority / Order (0=highest, blank=auto)", minimum=0
                )
                if isinstance(priority, _Cancelled):
                    continue
                members.append(
                    PoolMember(pick.model.id, rpm=rpm.value, priority=priority.value)
                )
            continue

        if result.kind == "edit":
            current = members[result.index]
            _clear()
            print(f"\n  Editing: {current.model}\n")
            rpm = _prompt_int("Requests per minute (RPM)", current.rpm)
            if isinstance(rpm, _Cancelled):
                continue
            priority = _prompt_int(
                "Priority / Order (0=highest, blank=auto)",
                current.priority,
                minimum=0,
            )
            if isinstance(priority, _Cancelled):
                continue
            members[result.index] = replace(
                current, rpm=rpm.value, priority=priority.value
            )
            continue


def _write_pools_conflict(
    pools: list[ModelPool], upstream_models: list[Model]
) -> None:
    last_error: Exception | None = None
    for _ in range(_MAX_CONFLICT_RECOVERY_ATTEMPTS):
        try:
            conflict_path = reserve_pools_conflict_path(POOLS_FILE)
        except (KeyboardInterrupt, Exception) as error:
            raise PoolSaveRecoveryError(
                pools,
                "Could not reserve a pool conflict artifact.",
            ) from error
        try:
            published = save_pools(
                pools,
                conflict_path,
                upstream_models=upstream_models,
            )
        except PoolSaveConflictError as error:
            last_error = error
            _release_conflict_path(conflict_path, reservation_digest())
            continue
        except (KeyboardInterrupt, Exception) as error:
            _release_conflict_path(conflict_path, reservation_digest())
            if conflict_artifact_carries_edits(conflict_path):
                print(f"  Your edits were written to {conflict_path.name}.")
                return
            raise PoolSaveRecoveryError(
                pools,
                "Could not preserve pending pool edits in a conflict artifact.",
            ) from error
        _release_conflict_path(conflict_path, published)
        if not conflict_artifact_holds(conflict_path, published):
            raise PoolSaveRecoveryError(
                pools,
                f"Could not confirm your pending pool edits in {conflict_path.name}.",
            )
        print(f"  Your edits were written to {conflict_path.name}.")
        return
    raise PoolSaveRecoveryError(
        pools,
        f"Could not preserve pending pool edits after {_MAX_CONFLICT_RECOVERY_ATTEMPTS} attempts.",
    ) from last_error


def _save_pools_or_recover(
    pools: list[ModelPool], upstream_models: list[Model]
) -> bool:
    while True:
        try:
            save_pools(pools, path=POOLS_FILE, upstream_models=upstream_models)
            return True
        except (KeyboardInterrupt, EOFError, PoolSaveUnreadableError):
            _write_pools_conflict(pools, upstream_models)
            return False
        except PoolSaveConflictError as error:
            _clear()
            print(f"\n  {error}\n")
            try:
                choice = input("  [o] Overwrite disk copy  [c] Cancel: ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                _write_pools_conflict(pools, upstream_models)
                return False
            if choice in {"o", "overwrite"}:
                try:
                    adopt_current_pools_digest(POOLS_FILE)
                except (KeyboardInterrupt, EOFError, PoolSaveUnreadableError):
                    _write_pools_conflict(pools, upstream_models)
                    return False
                continue
            _write_pools_conflict(pools, upstream_models)
            return False


def run_pool_manager(upstream_models: list[Model]) -> bool:
    """Interactive pool manager.  Returns *True* when data changed."""
    ensure_default_pools_file(POOLS_FILE)
    changed = False

    while True:
        try:
            pools = load_pools(POOLS_FILE, upstream_models=upstream_models)
        except RuntimeError as error:
            print(f"  {error}")
            print("  Leaving the pool manager so nothing is written over it.")
            return changed
        result = _pool_list_tui(pools)

        if result.kind == "back":
            return changed

        if result.kind == "add":
            _clear()
            print("\n  Create a new pool\n")
            name = _prompt_text("Pool name (used as model ID)")
            if not name:
                continue
            if any(p.name == name for p in pools) or any(
                m.id == name for m in upstream_models
            ):
                _clear()
                try:
                    input("  That name already exists. Press Enter...")
                except (KeyboardInterrupt, EOFError):
                    pass
                continue
            strategy = _prompt_strategy()
            if strategy is None:
                continue
            members = _edit_members_flow(name, [], upstream_models)
            if members is None:
                continue
            pools.append(ModelPool(name=name, members=members, strategy=strategy))
            changed = _save_pools_or_recover(pools, upstream_models) or changed

        elif result.kind == "edit" and pools:
            pool = pools[result.index]
            _clear()
            print(f"\n  Editing pool: {pool.name}\n")
            new_name = _prompt_text("Pool name", pool.name)
            if not new_name:
                continue
            if new_name != pool.name and (
                any(p.name == new_name for p in pools)
                or any(m.id == new_name for m in upstream_models)
            ):
                _clear()
                try:
                    input("  That name already exists. Press Enter...")
                except (KeyboardInterrupt, EOFError):
                    pass
                continue
            strategy = _prompt_strategy(pool.strategy)
            if strategy is None:
                continue
            members = _edit_members_flow(
                new_name, list(pool.members), upstream_models
            )
            if members is None:
                continue
            pools[result.index] = ModelPool(
                name=new_name, members=members, enabled=pool.enabled,
                strategy=strategy,
            )
            changed = _save_pools_or_recover(pools, upstream_models) or changed

        elif result.kind == "toggle" and pools:
            pool = pools[result.index]
            pools[result.index] = ModelPool(
                name=pool.name,
                members=pool.members,
                enabled=not pool.enabled,
                strategy=pool.strategy,
            )
            changed = _save_pools_or_recover(pools, upstream_models) or changed

        elif result.kind == "delete" and pools:
            pools.pop(result.index)
            changed = _save_pools_or_recover(pools, upstream_models) or changed
