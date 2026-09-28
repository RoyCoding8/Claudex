from __future__ import annotations

import contextlib
import errno
import io
import json
import os
import signal
import stat
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from modules import pools as pools_module
from modules.models import Model
from modules.pool_tui import (
    _Answered,
    _Cancelled,
    _edit_members_flow,
    _member_editor_tui,
    _MemberAction,
    _pool_list_tui,
    _PoolAction,
    _prompt_int,
    _save_pools_or_recover,
    _write_pools_conflict,
    run_pool_manager,
)
from modules.pools import (
    _LOADED_DIGESTS,
    ModelPool,
    PoolMember,
    PoolSaveConflictError,
    PoolSaveRecoveryError,
    PoolSaveUnreadableError,
    adopt_current_pools_digest,
    load_pools,
    reserve_pools_conflict_path,
    save_pools,
)


def _next_prompt_value(values, next_value, label):
    value = next_value(values, label)
    if isinstance(value, type) and issubclass(value, BaseException):
        raise value()
    if isinstance(value, BaseException):
        raise value
    return str(value)


_TUI_EXIT_DEADLINE_SECONDS = 10


@unittest.skipUnless(hasattr(signal, "SIGALRM"), "requires POSIX SIGALRM")
class PoolTuiExitTests(unittest.TestCase):
    def _run_with_key(self, surface, key: str):
        def on_timeout(_signum, _frame):
            raise AssertionError(f"pool TUI did not exit after {key!r}")

        with create_pipe_input() as pipe_input:
            pipe_input.send_text(key)
            with create_app_session(input=pipe_input, output=DummyOutput()):
                previous = signal.signal(signal.SIGALRM, on_timeout)
                signal.setitimer(signal.ITIMER_REAL, _TUI_EXIT_DEADLINE_SECONDS)
                try:
                    return surface()
                finally:
                    signal.setitimer(signal.ITIMER_REAL, 0)
                    signal.signal(signal.SIGALRM, previous)

    def test_pool_list_ctrl_c_and_ctrl_d_return_back(self) -> None:
        for key in ("\x03", "\x04"):
            with self.subTest(key=key):
                result = self._run_with_key(lambda: _pool_list_tui([]), key)
                self.assertEqual((result.kind, result.index), ("back", -1))

    def test_member_editor_ctrl_c_and_ctrl_d_return_cancel(self) -> None:
        for key in ("\x03", "\x04"):
            with self.subTest(key=key):
                result = self._run_with_key(
                    lambda: _member_editor_tui("pool", []), key
                )
                self.assertEqual((result.kind, result.index), ("cancel", -1))


class PoolManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.models = [
            Model("provider/alpha", "provider"),
            Model("provider/beta", "provider"),
        ]
        _LOADED_DIGESTS.clear()

    def tearDown(self) -> None:
        _LOADED_DIGESTS.clear()

    def _run_manager(
        self,
        initial_pools,
        actions,
        *,
        names=(),
        strategies=(),
        member_results=(),
        conflict_on_save=False,
        save_error=None,
        recovery_choices=("c",),
        conflict_artifacts=None,
        digest_adoptions=None,
    ):
        with tempfile.TemporaryDirectory() as directory:
            pools_path = Path(directory) / "pools.json"
            save_pools(initial_pools, pools_path)
            action_values = iter(actions)
            name_values = iter(names)
            strategy_values = iter(strategies)
            member_values = iter(member_results)
            recovery_values = iter(recovery_choices)
            snapshots = []
            persisted_snapshots = []
            conflict_artifacts = [] if conflict_artifacts is None else conflict_artifacts
            digest_adoptions = [] if digest_adoptions is None else digest_adoptions

            def next_value(values, label):
                try:
                    return next(values)
                except StopIteration:
                    self.fail(f"run_pool_manager requested an unexpected {label}")

            def load_for_manager(path, *, upstream_models):
                self.assertEqual(path, pools_path)
                return load_pools(pools_path, upstream_models=upstream_models)

            def save_for_worktree(pools, path=None, *, upstream_models):
                self.assertIs(upstream_models, self.models)
                self.assertIsNotNone(path)
                if path == pools_path and save_error is not None:
                    raise save_error
                if path == pools_path and conflict_on_save:
                    raise PoolSaveConflictError(
                        "Pool configuration changed on disk; reload before saving."
                    )
                published = save_pools(
                    pools,
                    path,
                    upstream_models=upstream_models,
                )
                if path == pools_path:
                    persisted_snapshots.append(
                        tuple(load_pools(pools_path, upstream_models=self.models))
                    )
                else:
                    conflict_artifacts.append(
                        (
                            path.name,
                            tuple(load_pools(path, upstream_models=self.models)),
                        )
                    )
                return published

            def show_pool_list(pools):
                snapshots.append(tuple(pools))
                return next_value(action_values, "pool action")

            with (
                patch("modules.pool_tui.ensure_default_pools_file"),
                patch("modules.pool_tui.POOLS_FILE", pools_path),
                patch("modules.pool_tui.load_pools", side_effect=load_for_manager),
                patch("modules.pool_tui.save_pools", side_effect=save_for_worktree),
                patch(
                    "modules.pool_tui.adopt_current_pools_digest",
                    side_effect=lambda: digest_adoptions.append(pools_path),
                ),
                patch("modules.pool_tui._pool_list_tui", side_effect=show_pool_list),
                patch("modules.pool_tui._clear"),
                patch(
                    "builtins.input",
                    side_effect=lambda _: _next_prompt_value(
                        recovery_values, next_value, "recovery choice"
                    ),
                ),
                patch(
                    "modules.pool_tui._prompt_text",
                    side_effect=lambda *_: next_value(name_values, "pool name"),
                ),
                patch(
                    "modules.pool_tui._prompt_strategy",
                    side_effect=lambda *_: next_value(strategy_values, "strategy"),
                ),
                patch(
                    "modules.pool_tui._edit_members_flow",
                    side_effect=lambda *_: next_value(member_values, "member result"),
                ),
            ):
                changed = run_pool_manager(self.models)

            return changed, snapshots, persisted_snapshots

    def test_add_edit_toggle_and_delete_save_each_visible_change(self) -> None:
        alpha = PoolMember("provider/alpha", rpm=10)
        beta = PoolMember("provider/beta", rpm=20, priority=1)
        changed, snapshots, persisted_snapshots = self._run_manager(
            [],
            [
                _PoolAction("add"),
                _PoolAction("edit", 0),
                _PoolAction("toggle", 0),
                _PoolAction("delete", 0),
                _PoolAction("back"),
            ],
            names=("primary", "renamed"),
            strategies=("round-robin", "weighted"),
            member_results=((alpha,), (beta,)),
        )

        self.assertTrue(changed)
        self.assertEqual(
            snapshots,
            [
                (),
                (ModelPool("primary", (alpha,), strategy="round-robin"),),
                (
                    ModelPool(
                        "renamed",
                        (beta,),
                        strategy="weighted",
                    ),
                ),
                (
                    ModelPool(
                        "renamed",
                        (beta,),
                        enabled=False,
                        strategy="weighted",
                    ),
                ),
                (),
            ],
        )
        self.assertEqual(
            persisted_snapshots,
            [
                (ModelPool("primary", (alpha,), strategy="round-robin"),),
                (ModelPool("renamed", (beta,), strategy="weighted"),),
                (
                    ModelPool(
                        "renamed",
                        (beta,),
                        enabled=False,
                        strategy="weighted",
                    ),
                ),
                (),
            ],
        )

    def test_deleting_a_later_pool_keeps_the_earlier_ones(self) -> None:
        first = ModelPool("first", (PoolMember("provider/alpha"),))
        second = ModelPool("second", (PoolMember("provider/beta"),))
        third = ModelPool("third", (PoolMember("provider/gamma"),))
        changed, snapshots, _ = self._run_manager(
            [first, second, third],
            [_PoolAction("delete", 1), _PoolAction("back")],
        )
        self.assertTrue(changed)
        self.assertEqual(snapshots, [(first, second, third), (first, third)])

    def test_an_edit_the_manager_refuses_leaves_the_pools_untouched(self) -> None:
        primary = ModelPool("primary", (PoolMember("provider/alpha", rpm=10),))
        secondary = ModelPool("secondary", (PoolMember("provider/beta", rpm=20),))
        cases = (
            ("member editor cancelled on add", [], _PoolAction("add"),
             {"names": ("primary",), "strategies": ("round-robin",), "member_results": (None,)}),
            ("member editor cancelled", [primary], _PoolAction("edit", 0),
             {"names": ("renamed",), "strategies": ("weighted",), "member_results": (None,)}),
            ("conflicting save cancelled", [primary], _PoolAction("add"),
             {"names": ("secondary",), "strategies": ("round-robin",),
              "member_results": ((PoolMember("provider/beta", rpm=20),),),
              "conflict_on_save": True}),
            ("duplicate name offered on add", [primary], _PoolAction("add"),
             {"names": ("primary",)}),
            ("duplicate name offered on rename", [primary, secondary], _PoolAction("edit", 0),
             {"names": ("secondary",)}),
        )
        for name, pools, action, keywords in cases:
            with self.subTest(refused=name):
                changed, snapshots, persisted = self._run_manager(
                    pools, [action, _PoolAction("back")], **keywords
                )

                self.assertFalse(changed)
                self.assertEqual(snapshots, [tuple(pools), tuple(pools)])
                self.assertEqual(persisted, [])

    def test_unreadable_save_writes_conflict_without_adopting_digest(self) -> None:
        member = PoolMember("provider/alpha")
        expected = ModelPool("primary", (member,), strategy="round-robin")
        conflict_artifacts = []
        digest_adoptions = []
        changed, snapshots, persisted_snapshots = self._run_manager(
            [],
            [_PoolAction("add"), _PoolAction("back")],
            names=("primary",),
            strategies=("round-robin",),
            member_results=((member,),),
            save_error=PoolSaveUnreadableError("Pool configuration is unreadable"),
            recovery_choices=("o", "c"),
            conflict_artifacts=conflict_artifacts,
            digest_adoptions=digest_adoptions,
        )

        self.assertFalse(changed)
        self.assertEqual(snapshots, [(), ()])
        self.assertEqual(persisted_snapshots, [])
        self.assertEqual(conflict_artifacts, [("pools.conflict.json", (expected,))])
        self.assertEqual(digest_adoptions, [])

    def test_duplicate_acknowledgement_cancel_returns_to_the_list(self) -> None:
        primary = ModelPool("primary", (PoolMember("provider/alpha", rpm=10),))
        secondary = ModelPool("secondary", (PoolMember("provider/beta", rpm=20),))
        for pools, action, name in (
            ([primary], _PoolAction("add"), "primary"),
            ([primary, secondary], _PoolAction("edit", 0), "secondary"),
        ):
            for error in (KeyboardInterrupt, EOFError):
                with self.subTest(action=action.kind, name=name, error=error):
                    changed, snapshots, persisted_snapshots = self._run_manager(
                        pools,
                        [action, _PoolAction("back")],
                        names=(name,),
                        recovery_choices=(error,),
                    )
                    self.assertFalse(changed)
                    self.assertEqual(snapshots, [tuple(pools), tuple(pools)])
                    self.assertEqual(persisted_snapshots, [])

    def test_unrelated_save_error_propagates(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "disk full"):
            self._run_manager(
                [],
                [_PoolAction("add")],
                names=("primary",),
                strategies=("round-robin",),
                member_results=((PoolMember("provider/alpha"),),),
                save_error=RuntimeError("disk full"),
            )


class MemberPromptTests(unittest.TestCase):
    def _run_member_action(self, actions, members, prompt_values, *, input_values=()):
        model = Model("provider/model", "provider")
        picker = SimpleNamespace(action="launch", model=model)
        remaining = iter(actions)
        values = iter((*input_values, *prompt_values))

        def editor_tui(_pool_name, current):
            action = next(remaining)
            if action.kind == "save":
                return _MemberAction("save", members=tuple(current))
            return action

        def input_value(_prompt):
            value = next(values)
            if isinstance(value, type) and issubclass(value, BaseException):
                raise value()
            if isinstance(value, BaseException):
                raise value
            return str(value)

        with (
            patch("modules.pool_tui._member_editor_tui", side_effect=editor_tui),
            patch("modules.pool_tui._clear"),
            patch("modules.tui.run_picker", return_value=picker),
            patch("builtins.input", side_effect=input_value),
        ):
            return _edit_members_flow("pool", members, [model])

    def test_add_publishes_the_prompted_member_and_a_cancel_discards_it(self) -> None:
        saved = self._run_member_action(
            [_MemberAction("add"), _MemberAction("save")],
            [],
            (20, 3),
        )
        self.assertEqual(saved, (PoolMember("provider/model", rpm=20, priority=3),))

        for prompt_values in ((KeyboardInterrupt,), (20, EOFError)):
            with self.subTest(prompt_values=prompt_values):
                members: list[PoolMember] = []
                cancelled = self._run_member_action(
                    [_MemberAction("add"), _MemberAction("cancel")],
                    members,
                    prompt_values,
                )
                self.assertIsNone(cancelled)
                self.assertEqual(members, [])

    def test_edit_publishes_the_prompted_values_and_keeps_the_unprompted_ones(self) -> None:
        original = PoolMember("provider/model", rpm=10, priority=2, limit=30, cooldown=15.0)
        saved = self._run_member_action(
            [_MemberAction("edit", 0), _MemberAction("save")],
            [original],
            (30, 4),
        )
        self.assertEqual(
            saved,
            (PoolMember("provider/model", rpm=30, priority=4, limit=30, cooldown=15.0),),
        )

        for prompt_values in ((KeyboardInterrupt,), (30, EOFError)):
            with self.subTest(prompt_values=prompt_values):
                members = [original]
                cancelled = self._run_member_action(
                    [_MemberAction("edit", 0), _MemberAction("cancel")],
                    members,
                    prompt_values,
                )
                self.assertIsNone(cancelled)
                self.assertEqual(members, [original])

    def test_duplicate_add_returns_to_the_editor_with_the_member_unchanged(self) -> None:
        original = PoolMember("provider/model", rpm=10)
        for acknowledgement in ("", KeyboardInterrupt, EOFError):
            with self.subTest(acknowledgement=acknowledgement):
                saved = self._run_member_action(
                    [_MemberAction("add"), _MemberAction("save")],
                    [original],
                    (),
                    input_values=(acknowledgement,),
                )
                self.assertEqual(saved, (original,))

    def test_enter_at_both_prompts_of_an_existing_member_keeps_its_values(self) -> None:
        original = PoolMember("provider/model", rpm=10, priority=2)
        saved = self._run_member_action(
            [_MemberAction("edit", 0), _MemberAction("save")],
            [original],
            ("", ""),
        )
        self.assertEqual(saved, (original,))

    def test_enter_at_the_prompts_of_a_new_member_leaves_both_unset(self) -> None:
        saved = self._run_member_action(
            [_MemberAction("add"), _MemberAction("save")],
            [],
            ("", ""),
        )
        self.assertEqual(saved, (PoolMember("provider/model"),))

    def test_a_prompt_answer_the_pool_limit_rejects_is_reprompted(self) -> None:
        for rejected in (str(2**63), "0", "twelve"):
            with self.subTest(answer=rejected), patch(
                "builtins.input", side_effect=(rejected, "10")
            ):
                self.assertEqual(_prompt_int("Requests per minute (RPM)"), _Answered(10))

    def test_prompt_result_variants_admit_no_contradictory_state(self) -> None:
        for attribute in ("value", "cancelled"):
            with self.subTest(attribute=attribute, variant="answered"):
                with self.assertRaises((AttributeError, TypeError)):
                    setattr(_Answered(5), attribute, 1)
            with self.subTest(attribute=attribute, variant="cancelled"):
                with self.assertRaises((AttributeError, TypeError)):
                    setattr(_Cancelled(), attribute, 1)
        self.assertEqual(_Answered(5).value, 5)
        self.assertEqual(_Answered(None).value, None)
        self.assertNotIsInstance(_Answered(5), _Cancelled)
        self.assertNotIsInstance(_Cancelled(), _Answered)


class ConflictRecoveryTests(unittest.TestCase):
    _EXTERNAL = '{"pools": [{"name":"external","members":[]}]}\n'
    _UNREADABLE = '{"version": 99, "pools": "not-a-list"}\n'

    def tearDown(self) -> None:
        _LOADED_DIGESTS.clear()

    def _conflicted_pools_file(self, directory: str) -> Path:
        """A pools file whose on-disk copy was replaced behind the loaded digest."""
        pools_path = Path(directory) / "pools.json"
        pools_path.write_text('{"pools": []}\n', encoding="utf-8")
        load_pools(pools_path)
        pools_path.write_text(self._EXTERNAL, encoding="utf-8")
        return pools_path

    def test_repeated_recoveries_each_keep_the_edits_and_leak_no_lock_or_digest(self) -> None:
        models = [Model("provider/alpha", "provider")]
        pools = [ModelPool("primary", (PoolMember("provider/alpha"),))]
        for events in (1, 5, 10, 20):
            with self.subTest(events=events), tempfile.TemporaryDirectory() as directory:
                _LOADED_DIGESTS.clear()
                pools_path = self._conflicted_pools_file(directory)
                with (
                    patch("modules.pool_tui.POOLS_FILE", pools_path),
                    patch("builtins.input", return_value="c"),
                ):
                    for _ in range(events):
                        pools_path.write_text('{"pools": []}\n', encoding="utf-8")
                        load_pools(pools_path)
                        pools_path.write_text(self._EXTERNAL, encoding="utf-8")
                        self.assertFalse(_save_pools_or_recover(pools, models))

                artifacts = sorted(Path(directory).glob("pools.conflict*.json"))
                locks = sorted(entry.name for entry in Path(directory).glob(".*.lock"))
                digests = {entry.resolve() for entry in _LOADED_DIGESTS}
                self.assertEqual(len(artifacts), events)
                self.assertEqual(
                    locks,
                    sorted(
                        [".pools.json.lock"]
                        + [f".{artifact.name}.lock" for artifact in artifacts]
                    ),
                )
                self.assertEqual(
                    {entry.stat().st_size for entry in Path(directory).glob(".*.lock")},
                    {0},
                )
                self.assertEqual(digests, {pools_path.resolve()})
                self.assertEqual(
                    [load_pools(artifact, upstream_models=models) for artifact in artifacts],
                    [pools] * events,
                )

    def test_a_reservation_that_cannot_complete_is_reported_as_a_reservation_failure(self) -> None:
        pools = [ModelPool("primary", (PoolMember("provider/alpha"),))]
        attempts = 0

        def no_space(_path):
            nonlocal attempts
            attempts += 1
            raise OSError(28, "No space left on device")

        real_close = os.close

        def cancel(_file_descriptor: int) -> None:
            # os.close cannot be interrupted mid-call; close-then-raise is the
            # only ordering a caller can observe.
            real_close(_file_descriptor)
            raise KeyboardInterrupt

        for name, target, reserve, expected_attempts in (
            ("interrupted-close", "modules.pools.os.close", cancel, 0),
            ("no-space", "modules.pool_tui.reserve_pools_conflict_path", no_space, 1),
        ):
            with self.subTest(reservation=name), tempfile.TemporaryDirectory() as directory:
                attempts = 0
                pools_path = Path(directory) / "pools.json"
                with (
                    patch("modules.pool_tui.POOLS_FILE", pools_path),
                    patch(target, side_effect=reserve),
                    self.assertRaises(PoolSaveRecoveryError) as caught,
                ):
                    _write_pools_conflict(pools, [])
                self.assertEqual(attempts, expected_attempts)
                self.assertEqual(
                    str(caught.exception), "Could not reserve a pool conflict artifact.")
                self.assertEqual(caught.exception.pools, tuple(pools))
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_a_faulted_save_leaves_the_pending_edits_in_a_recovery_artifact(self) -> None:
        models = [Model("provider/alpha", "provider")]
        pools = [ModelPool("primary", (PoolMember("provider/alpha"),))]
        real_fsync, real_mkstemp, real_replace = os.fsync, tempfile.mkstemp, os.replace

        def first_publish_only(real):
            def build(fault, _pools_path):
                state = [0]
                fired = []

                def side_effect(*args, **kwargs):
                    state[0] += 1
                    if state[0] == 1:
                        fired.append(state[0])
                        raise fault
                    return real(*args, **kwargs)

                side_effect.fired = fired
                return side_effect
            return build

        def dead_descriptor_once(real):
            def build(_error, _pools_path):
                state = [0]
                fired = []

                def side_effect(*args, **kwargs):
                    state[0] += 1
                    descriptor, name = real(*args, **kwargs)
                    if state[0] == 1:
                        fired.append(state[0])
                        os.close(descriptor)
                    return descriptor, name

                side_effect.fired = fired
                return side_effect
            return build

        def live_pools_file_only(real):
            def build(fault, pools_path):
                fired = []

                def side_effect(source, target):
                    if Path(os.path.realpath(target)) == Path(os.path.realpath(pools_path)):
                        fired.append(target)
                        raise fault
                    return real(source, target)

                side_effect.fired = fired
                return side_effect
            return build

        cases = (
            ("fsync-enospc", errno.ENOSPC, "modules.pools.os.fsync", first_publish_only(real_fsync)),
            ("fsync-eio", errno.EIO, "modules.pools.os.fsync", first_publish_only(real_fsync)),
            ("mkstemp-enospc", errno.ENOSPC, "modules.pools.tempfile.mkstemp",
             first_publish_only(real_mkstemp)),
            ("fdopen-ebadf", errno.EBADF, "modules.pools.tempfile.mkstemp",
             dead_descriptor_once(real_mkstemp)),
            ("replace-eio", errno.EIO, "modules.pools.os.replace", live_pools_file_only(real_replace)),
        )
        for name, code, target, build in cases:
            with self.subTest(fault=name), tempfile.TemporaryDirectory() as directory:
                pools_path = Path(directory) / "pools.json"
                faulted = build(OSError(code, os.strerror(code)), pools_path)
                with (
                    patch("modules.pool_tui.POOLS_FILE", pools_path),
                    patch(target, side_effect=faulted),
                ):
                    result = _save_pools_or_recover(pools, models)

                if fired := getattr(faulted, "fired", None):
                    self.assertTrue(fired, f"{name} was never faulted, so nothing was tested")
                self.assertFalse(result, f"{name} reported a published save")
                artifacts = list(Path(directory).glob("pools.conflict*.json"))
                self.assertEqual(len(artifacts), 1, f"{name} left {artifacts}")
                self.assertEqual(load_pools(artifacts[0], upstream_models=models), pools)
                self.assertFalse(pools_path.exists(), f"{name} left a partial pools.json")

    def test_an_artifact_that_cannot_be_published_returns_the_edits_and_writes_nothing(self) -> None:
        models = [Model("provider/alpha", "provider")]
        pools = [ModelPool("primary", (PoolMember("provider/alpha"),))]

        def cancel(_pools, _path, *, upstream_models):
            raise KeyboardInterrupt

        def disk_full(_pools, path, *, upstream_models):
            if Path(path).name == "pools.json":
                raise PoolSaveConflictError("Pool save conflicted")
            raise OSError("disk full")

        for name, entry, fault in (
            ("cancelled", _write_pools_conflict, cancel),
            ("disk-full", _save_pools_or_recover, disk_full),
        ):
            with self.subTest(artifact=name), tempfile.TemporaryDirectory() as directory:
                pools_path = self._conflicted_pools_file(directory)
                with (
                    patch("modules.pool_tui.POOLS_FILE", pools_path),
                    patch("modules.pool_tui.save_pools", side_effect=fault),
                    patch("builtins.input", return_value="c"),
                    self.assertRaises(PoolSaveRecoveryError) as caught,
                ):
                    entry(pools, models)
                self.assertEqual(caught.exception.pools, tuple(pools))
                self.assertEqual(
                    str(caught.exception),
                    "Could not preserve pending pool edits in a conflict artifact.",
                )
                self.assertEqual(list(Path(directory).glob("pools.conflict*.json")), [])

    def test_only_an_adoptable_overwrite_retries_and_every_other_answer_cancels(self) -> None:
        models = [Model("provider/alpha", "provider")]
        pools = [ModelPool("primary", (PoolMember("provider/alpha"),))]
        cases = (
            ("overwrite", "o", None, "adopt", True),
            ("overwrite-unreadable", "o", self._UNREADABLE, "adopt", False),
            ("overwrite-cancelled", "o", None, "cancel", False),
            ("blank", "", None, "-", False),
            ("whitespace", "   ", None, "-", False),
            ("unrecognized", "maybe", None, "-", False),
            ("interrupted", KeyboardInterrupt, None, "-", False),
            ("eof", EOFError, None, "-", False),
        )
        for name, answer, disk, adoption, expected_result in cases:
            with self.subTest(answer=name), tempfile.TemporaryDirectory() as directory:
                pools_path = self._conflicted_pools_file(directory)
                surviving_copy = self._EXTERNAL
                if disk is not None:
                    pools_path.write_text(disk, encoding="utf-8")
                    surviving_copy = disk
                adopted: list[Path] = []

                def adopt_worktree_path(
                    path: Path, _adopted=adopted, _adoption=adoption
                ) -> None:
                    _adopted.append(path)
                    if _adoption == "cancel":
                        raise KeyboardInterrupt
                    adopt_current_pools_digest(path)

                prompt = ({"side_effect": answer} if isinstance(answer, type)
                          else {"return_value": answer})
                with (
                    patch("modules.pool_tui.POOLS_FILE", pools_path),
                    patch("modules.pool_tui.save_pools", side_effect=save_pools),
                    patch("builtins.input", **prompt),
                    patch("modules.pool_tui.adopt_current_pools_digest",
                          side_effect=adopt_worktree_path),
                ):
                    result = _save_pools_or_recover(pools, models)

                self.assertEqual(adopted, [] if adoption == "-" else [pools_path])
                self.assertEqual(result, expected_result)
                artifacts = list(Path(directory).glob("pools.conflict*.json"))
                self.assertEqual(len(artifacts), 0 if expected_result else 1)
                if expected_result:
                    self.assertEqual(load_pools(pools_path, upstream_models=models), pools)
                else:
                    self.assertEqual(
                        load_pools(artifacts[0], upstream_models=models), pools)
                    self.assertEqual(pools_path.read_text(encoding="utf-8"), surviving_copy)

    def test_an_exhausted_recovery_reports_the_attempts_and_releases_every_reservation(self) -> None:
        pools = [ModelPool("primary", (PoolMember("provider/alpha"),))]
        reserved: list[Path] = []
        real_reserve = pools_module.reserve_pools_conflict_path
        before = {entry.resolve() for entry in _LOADED_DIGESTS}

        def record(path: Path) -> Path:
            reserved.append(real_reserve(path))
            return reserved[-1]

        with tempfile.TemporaryDirectory() as directory:
            with (
                patch("modules.pool_tui.POOLS_FILE", Path(directory) / "pools.json"),
                patch("modules.pool_tui.reserve_pools_conflict_path", side_effect=record),
                patch("modules.pool_tui.save_pools",
                      side_effect=PoolSaveConflictError("still changing")),
                self.assertRaises(PoolSaveRecoveryError) as caught,
            ):
                _write_pools_conflict(pools, [])

            self.assertEqual(len(reserved), 8)
            self.assertEqual(
                str(caught.exception),
                "Could not preserve pending pool edits after 8 attempts.",
            )
            self.assertEqual(
                [entry.name for entry in Path(directory).iterdir()
                 if entry.name.endswith(".json")],
                [],
            )
            self.assertEqual({entry.resolve() for entry in _LOADED_DIGESTS} - before, set())

    def test_a_recovery_names_only_the_new_artifact_and_keeps_every_existing_one(self) -> None:
        models = [Model("provider/alpha", "provider")]
        pools = [ModelPool("primary", (PoolMember("provider/alpha"),))]
        occupied = '{"pools": []}\n'
        foreign = '{"prior":"conflict"}\n'
        cases = (
            ("none", ()),
            ("three-pool-files", (occupied, occupied, occupied)),
            ("one-foreign-file", (foreign,)),
        )
        for name, priors in cases:
            with self.subTest(occupied_by=name), tempfile.TemporaryDirectory() as directory:
                pools_path = self._conflicted_pools_file(directory)
                for index, content in enumerate(priors):
                    (Path(directory) /
                     f"pools.conflict{'' if index == 0 else f'.{index}'}.json").write_text(
                        content, encoding="utf-8")
                printed = io.StringIO()
                with (
                    patch("modules.pool_tui.POOLS_FILE", pools_path),
                    patch("modules.pool_tui.save_pools", side_effect=save_pools),
                    patch("builtins.input", return_value="c"),
                    redirect_stdout(printed),
                ):
                    self.assertFalse(_save_pools_or_recover(pools, models))

                for index, content in enumerate(priors):
                    self.assertEqual(
                        (Path(directory) /
                         f"pools.conflict{'' if index == 0 else f'.{index}'}.json").read_text(
                            encoding="utf-8"),
                        content,
                    )
                suffix = "" if not priors else f".{len(priors)}"
                written = Path(directory) / f"pools.conflict{suffix}.json"
                self.assertEqual(load_pools(written, upstream_models=models), pools)
                self.assertIn(written.name, printed.getvalue())
                self.assertEqual(pools_path.read_text(encoding="utf-8"), self._EXTERNAL)

    def test_a_recovery_claims_the_artifact_only_while_it_still_carries_the_edits(self) -> None:
        models = [Model("provider/alpha", "provider")]
        pools = [ModelPool("primary", (PoolMember("provider/alpha"),))]
        real_release = pools_module._release_conflict_path

        def release_then_stomp(path, digest, _real=real_release):
            _real(path, digest)
            Path(path).unlink(missing_ok=True)

        def release_then_corrupt(path, digest, _real=real_release):
            _real(path, digest)
            Path(path).write_text('{"pools": []}\n', encoding="utf-8")

        cases = (
            ("released-untouched", None, None, False, 1),
            ("released-then-stomped", "modules.pool_tui._release_conflict_path",
             release_then_stomp, True, 0),
            ("released-then-corrupted", "modules.pool_tui._release_conflict_path",
             release_then_corrupt, True, 1),
        )
        for name, target, release, unconfirmed, expected_artifacts in cases:
            with self.subTest(after_publish=name), tempfile.TemporaryDirectory() as directory:
                pools_path = self._conflicted_pools_file(directory)
                printed = io.StringIO()
                with contextlib.ExitStack() as stack:
                    stack.enter_context(patch("modules.pool_tui.POOLS_FILE", pools_path))
                    stack.enter_context(patch("builtins.input", return_value="c"))
                    stack.enter_context(redirect_stdout(printed))
                    if release is not None:
                        stack.enter_context(patch(target, side_effect=release))
                    if unconfirmed:
                        stack.enter_context(self.assertRaises(PoolSaveRecoveryError))
                    result = _save_pools_or_recover(pools, models)

                artifacts = list(Path(directory).glob("pools.conflict*.json"))
                self.assertEqual(len(artifacts), expected_artifacts)
                if unconfirmed:
                    self.assertNotIn("Your edits were written to", printed.getvalue())
                else:
                    self.assertFalse(result)
                    self.assertEqual(
                        json.loads(artifacts[0].read_text(encoding="utf-8"))["pools"][0]["members"],
                        [{"model": "provider/alpha"}],
                    )
                    self.assertEqual(load_pools(artifacts[0], upstream_models=models), pools)
                    self.assertIn(artifacts[0].name, printed.getvalue())

    def test_an_interrupted_publish_never_forks_an_artifact_or_misreports_the_save(self) -> None:
        models = [Model("provider/alpha", "provider")]
        pools = [ModelPool("primary", (PoolMember("provider/alpha"),))]
        real_replace = os.replace
        real_publish = pools_module._atomic_publish

        def replace_then_cancel(source, target):
            real_replace(source, target)
            raise KeyboardInterrupt

        def publish_then_interrupt(*args, **kwargs):
            real_publish(*args, **kwargs)
            raise KeyboardInterrupt

        cases = (
            ("cancelled-at-the-replace", "modules.pools.os.replace", replace_then_cancel,
             False, True, 0),
            ("cancelled-after-the-publish", "modules.pools._atomic_publish",
             publish_then_interrupt, False, True, 0),
            ("cancelled-publishing-the-artifact", "modules.pools._atomic_publish",
             publish_then_interrupt, True, False, 1),
        )
        for name, target, fault, conflicted, expected_result, expected_artifacts in cases:
            with self.subTest(interrupted=name), tempfile.TemporaryDirectory() as directory:
                pools_path = (self._conflicted_pools_file(directory) if conflicted
                              else Path(directory) / "pools.json")
                with (
                    patch("modules.pool_tui.POOLS_FILE", pools_path),
                    patch("builtins.input", return_value="c"),
                    patch(target, side_effect=fault),
                ):
                    result = _save_pools_or_recover(pools, models)

                self.assertEqual(result, expected_result)
                if expected_result:
                    self.assertEqual(load_pools(pools_path, upstream_models=models), pools)
                else:
                    self.assertEqual(pools_path.read_text(encoding="utf-8"), self._EXTERNAL)
                artifacts = list(Path(directory).glob("pools.conflict*.json"))
                self.assertEqual(len(artifacts), expected_artifacts)
                if expected_artifacts:
                    self.assertEqual(load_pools(artifacts[0], upstream_models=models), pools)

    def test_a_failure_after_the_artifact_is_published_keeps_and_names_the_pending_edits(
        self,
    ) -> None:
        models = [Model("provider/alpha", "provider")]
        pools = [ModelPool("primary", (PoolMember("provider/alpha"),))]
        real_close = os.close
        real_read_digest = pools_module._read_current_digest

        def close_fails_on_a_directory(_pools_path: Path):
            def fault(file_descriptor: int) -> None:
                if stat.S_ISDIR(os.fstat(file_descriptor).st_mode):
                    raise OSError(5, "I/O error")
                return real_close(file_descriptor)
            return fault

        def eof_on_the_real_file(pools_path: Path):
            def fault(path: Path) -> tuple[bool, str]:
                if Path(path) == pools_path:
                    raise EOFError("stdin closed mid-save")
                return real_read_digest(path)
            return fault

        for name, target, fault in (
            ("directory-close", "modules.pools.os.close", close_fails_on_a_directory),
            ("digest-read", "modules.pools._read_current_digest", eof_on_the_real_file),
        ):
            with self.subTest(after_publish=name), tempfile.TemporaryDirectory() as directory:
                pools_path = self._conflicted_pools_file(directory)
                printed = io.StringIO()
                with (
                    patch("modules.pool_tui.POOLS_FILE", pools_path),
                    patch("builtins.input", return_value="c"),
                    patch(target, side_effect=fault(pools_path)),
                    redirect_stdout(printed),
                ):
                    self.assertFalse(_save_pools_or_recover(pools, models))

                survivors = [
                    artifact for artifact in sorted(Path(directory).glob("pools.conflict*.json"))
                    if load_pools(artifact, upstream_models=models) == pools
                ]
                self.assertEqual(len(survivors), 1, "the published pending edits were destroyed")
                self.assertIn(survivors[0].name, printed.getvalue())

    def test_conflict_artifact_allocation_is_collision_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pools_path = Path(directory) / "pools.json"

            def write_artifact(name: str) -> Path:
                artifact = reserve_pools_conflict_path(pools_path)
                save_pools([ModelPool(name, (PoolMember("provider/model"),))], artifact)
                return artifact

            with ThreadPoolExecutor(max_workers=2) as executor:
                artifacts = list(executor.map(write_artifact, ("first", "second")))

            self.assertEqual({artifact.name for artifact in artifacts}, {
                "pools.conflict.json", "pools.conflict.1.json",
            })
            self.assertEqual(
                {load_pools(artifact)[0].name for artifact in artifacts},
                {"first", "second"},
            )

    def test_conflict_save_passes_upstream_models_to_conflict_file(self) -> None:
        models = [Model("provider/alpha", "provider")]
        pools = [ModelPool("primary", (PoolMember("provider/alpha"),))]

        with tempfile.TemporaryDirectory() as directory:
            pools_path = Path(directory) / "pools.json"
            conflict_path = Path(directory) / "pools.conflict.json"

            def save_to_worktree_path(values, path, *, upstream_models):
                self.assertIs(upstream_models, models)
                if path == pools_path:
                    raise PoolSaveConflictError("Pool save conflicted")
                return save_pools(values, path, upstream_models=upstream_models)

            with (
                patch("modules.pool_tui.POOLS_FILE", pools_path),
                patch("modules.pool_tui.save_pools", side_effect=save_to_worktree_path),
                patch("builtins.input", return_value="c"),
            ):
                result = _save_pools_or_recover(pools, models)

            self.assertFalse(result)
            self.assertEqual(load_pools(conflict_path, upstream_models=models), pools)

    def test_cancelled_save_of_the_real_file_writes_a_conflict_artifact(self) -> None:
        models = [Model("provider/alpha", "provider")]
        pools = [ModelPool("primary", (PoolMember("provider/alpha"),))]
        with tempfile.TemporaryDirectory() as directory:
            pools_path = Path(directory) / "pools.json"
            pools_path.write_text('{"pools": []}\n', encoding="utf-8")
            load_pools(pools_path)

            def cancel_only_the_real_file(pools_arg, path, *, upstream_models):
                if Path(path) == pools_path:
                    raise KeyboardInterrupt
                return pools_module.save_pools(pools_arg, path, upstream_models=upstream_models)

            with (
                patch("modules.pool_tui.POOLS_FILE", pools_path),
                patch("modules.pool_tui.save_pools", side_effect=cancel_only_the_real_file),
            ):
                try:
                    result = _save_pools_or_recover(pools, models)
                except BaseException as escaped:
                    self.fail(f"the cancelled save escaped without a conflict artifact: {escaped!r}")

            self.assertFalse(result)
            self.assertEqual(
                load_pools(Path(directory) / "pools.conflict.json", upstream_models=models),
                pools,
            )
            self.assertEqual(load_pools(pools_path, upstream_models=models), [])

    def test_default_publish_survives_a_cancelled_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pools_path = root / "pools.json"
            example = root / "pools.example.json"
            example.write_text(
                '{"version": 1, "pools": [{"name": "seeded", "members": [{"model": "a/x"}]}]}\n',
                encoding="utf-8",
            )
            with (
                patch("modules.pools.POOLS_FILE", pools_path),
                patch("modules.pools.POOLS_EXAMPLE_FILE", example),
                patch("modules.pools.os.unlink", side_effect=KeyboardInterrupt),
            ):
                try:
                    pools_module.ensure_default_pools_file()
                except BaseException as escaped:
                    self.fail(f"the cancelled cleanup escaped the release as {escaped!r}")

            self.assertEqual(
                load_pools(pools_path),
                [ModelPool("seeded", (PoolMember("a/x"),))],
            )
            self.assertEqual(
                [entry.name for entry in root.iterdir() if entry.name.endswith(".tmp")],
                [],
            )


class PoolManagerUnreadableConfigTests(unittest.TestCase):
    def test_a_config_that_becomes_unreadable_leaves_the_manager_without_writing(self) -> None:
        printed = []
        with tempfile.TemporaryDirectory() as directory, \
             patch("modules.pool_tui.POOLS_FILE", Path(directory) / "pools.json"), \
             patch("modules.pool_tui.ensure_default_pools_file"), \
             patch("modules.pool_tui._pool_list_tui") as listing, \
             patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(str(x) for x in a))):
            path = Path(directory) / "pools.json"
            path.write_text("not json", encoding="utf-8")
            changed = run_pool_manager([])
            survived = path.read_text(encoding="utf-8")

        self.assertFalse(changed)
        listing.assert_not_called()
        self.assertIn("Leaving the pool manager", "\n".join(printed))
        self.assertEqual(survived, "not json")


if __name__ == "__main__":
    unittest.main()
