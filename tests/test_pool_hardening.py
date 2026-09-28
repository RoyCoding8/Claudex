from __future__ import annotations

import json
import multiprocessing
import os
import stat
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from modules import pools as pools_module
from modules.models import Model
from modules.pools import (
    _LOADED_DIGESTS,
    ModelPool,
    PoolMember,
    PoolSaveConflictError,
    PoolSaveUnreadableError,
    _pool_file_lock,
    _release_conflict_path,
    adopt_current_pools_digest,
    conflict_artifact_carries_edits,
    conflict_artifact_holds,
    load_pools,
    reservation_digest,
    reserve_pools_conflict_path,
    save_pools,
)


def _symlinks_available() -> bool:
    """Whether this account may create a symlink.

    Not a platform fact: it needs Developer Mode or elevation on Windows.
    """
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / "target"
        target.touch()
        try:
            (Path(directory) / "alias").symlink_to(target)
        except OSError:
            return False
        return True


requires_symlinks = unittest.skipUnless(
    _symlinks_available(), "creating a symlink needs Developer Mode or elevation"
)


def _process_save_worker(path: str, pool: ModelPool, barrier, results) -> None:
    try:
        load_pools(Path(path))
        barrier.wait()
        save_pools([pool], Path(path))
    except PoolSaveConflictError:
        results.put("conflict")
    except Exception as error:
        results.put(type(error).__name__)
    else:
        results.put("saved")


class PoolHardeningTests(unittest.TestCase):
    def tearDown(self) -> None:
        _LOADED_DIGESTS.clear()

    def test_pool_lock_requires_an_interprocess_primitive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pools.json"
            with (
                patch("modules.pools.fcntl_module", None),
                patch("modules.pools.msvcrt_module", None),
                self.assertRaisesRegex(RuntimeError, "interprocess"),
            ):
                with _pool_file_lock(path):
                    self.fail("lock should not be available")

    @requires_symlinks
    def test_symlink_alias_uses_canonical_lock_and_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "pools.json"
            alias = root / "pools-alias.json"
            pools = [ModelPool("p", (PoolMember("a/x"), PoolMember("b/x")))]
            save_pools([], target)
            alias.symlink_to(target)
            load_pools(alias)
            save_pools(pools, alias)
            self.assertEqual(load_pools(target), pools)
            self.assertEqual(set(_LOADED_DIGESTS), {target.resolve()})

    def test_default_publish_rechecks_under_pool_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "pools.json"
            example = root / "pools.example.json"
            external = '{"version": 1, "pools": [{"name": "external", "members": []}]}\n'
            example.write_text('{"version": 1, "pools": []}\n', encoding="utf-8")
            lock_paths = []

            from contextlib import contextmanager

            real_lock = pools_module._pool_file_lock
            real_read = pools_module._read_pool_text

            @contextmanager
            def observed_lock(path):
                lock_paths.append(path)
                with real_lock(path):
                    yield

            def read_then_publish(path):
                text = real_read(path)
                if path == example:
                    target.write_text(external, encoding="utf-8")
                return text

            with (
                patch("modules.pools.POOLS_FILE", target),
                patch("modules.pools.POOLS_EXAMPLE_FILE", example),
                patch("modules.pools._pool_file_lock", observed_lock),
                patch("modules.pools._read_pool_text", side_effect=read_then_publish),
            ):
                pools_module.ensure_default_pools_file()

            self.assertEqual(lock_paths, [target.resolve()])
            self.assertEqual(target.read_text(encoding="utf-8"), external)

    def test_default_publish_keeps_a_writer_that_lands_before_the_swap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "pools.json"
            example = root / "pools.example.json"
            external = '{"version": 1, "pools": [{"name": "external", "members": [{"model": "b/x"}]}]}\n'
            example.write_text('{"version": 1, "pools": []}\n', encoding="utf-8")
            real_fsync = os.fsync

            def writer_lands_mid_publish(file_descriptor: int) -> None:
                real_fsync(file_descriptor)
                target.write_text(external, encoding="utf-8")

            with (
                patch("modules.pools.POOLS_FILE", target),
                patch("modules.pools.POOLS_EXAMPLE_FILE", example),
                patch("modules.pools.os.fsync", side_effect=writer_lands_mid_publish),
            ):
                pools_module.ensure_default_pools_file()

            self.assertEqual(target.read_text(encoding="utf-8"), external)
            self.assertEqual([pool.name for pool in load_pools(target)], ["external"])

    def test_default_publish_survives_a_cancellation_after_the_rename(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "pools.json"
            example = root / "pools.example.json"
            example.write_text(
                '{"version": 1, "pools": [{"name": "seeded", "members": [{"model": "a/x"}]}]}\n',
                encoding="utf-8",
            )
            real_replace = os.replace

            def replace_then_cancel(source, target_path, _error=KeyboardInterrupt, _real=real_replace):
                _real(source, target_path)
                raise _error

            with (
                patch("modules.pools.POOLS_FILE", target),
                patch("modules.pools.POOLS_EXAMPLE_FILE", example),
                patch("modules.pools.os.replace", side_effect=replace_then_cancel),
            ):
                try:
                    pools_module.ensure_default_pools_file()
                except BaseException as escaped:
                    self.fail(f"the publish cancellation escaped as {escaped!r}")

            self.assertEqual(load_pools(target), [ModelPool("seeded", (PoolMember("a/x"),))])
            later = [ModelPool("later", (PoolMember("b/y"),))]
            save_pools(later, target)
            self.assertEqual(load_pools(target), later)

    def test_cancellation_before_the_rename_does_not_report_a_published_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pools.json"
            pools = [ModelPool("p", (PoolMember("a/x"),))]

            def cancel_without_renaming(_source, _target):
                raise KeyboardInterrupt

            with (
                patch("modules.pools.os.replace", side_effect=cancel_without_renaming),
                self.assertRaises(KeyboardInterrupt),
            ):
                save_pools(pools, path)

            self.assertFalse(path.exists())
            self.assertEqual(
                [entry.name for entry in Path(directory).iterdir()
                 if entry.name.endswith(".tmp")],
                [],
            )
            self.assertNotIn(path.resolve(), _LOADED_DIGESTS)

    def test_a_cancel_before_fdopen_takes_ownership_closes_the_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pools.json"
            pools = [ModelPool("p", (PoolMember("a/x"),))]
            real_mkstemp = tempfile.mkstemp
            handed_over: list[tuple[int, str]] = []
            closed: list[int] = []
            real_close = os.close

            def remember(*args, **kwargs):
                handed_over.append(real_mkstemp(*args, **kwargs))
                return handed_over[-1]

            def cancel_at_fdopen(*args, **kwargs):
                raise KeyboardInterrupt

            def note_close(descriptor: int) -> None:
                closed.append(descriptor)
                real_close(descriptor)

            with (
                patch("modules.pools.tempfile.mkstemp", side_effect=remember),
                patch("modules.pools.os.fdopen", side_effect=cancel_at_fdopen),
                patch("modules.pools.os.close", side_effect=note_close),
                self.assertRaises(KeyboardInterrupt),
            ):
                save_pools(pools, path)

            self.assertEqual(len(handed_over), 1)
            descriptor, _temporary = handed_over[0]
            self.assertIn(descriptor, closed)
            with self.assertRaises(OSError):
                os.fstat(descriptor)
            self.assertEqual(
                [entry.name for entry in Path(directory).iterdir()
                 if entry.name.endswith(".tmp")],
                [],
            )
            self.assertFalse(path.exists())

    def test_cancellation_after_the_rename_does_report_a_published_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pools.json"
            pools = [ModelPool("p", (PoolMember("a/x"),))]
            real_replace = os.replace

            def replace_then_cancel(source, target):
                real_replace(source, target)
                raise KeyboardInterrupt

            with patch("modules.pools.os.replace", side_effect=replace_then_cancel):
                self.assertEqual(save_pools(pools, path), pools_module._digest(
                    path.read_text(encoding="utf-8")))

            self.assertEqual(load_pools(path), pools)

    def test_default_publish_stays_quiet_when_the_file_cannot_be_read(self) -> None:
        for error in (PoolSaveUnreadableError, PoolSaveConflictError):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                target = root / "pools.json"
                example = root / "pools.example.json"
                example.write_text(
                    '{"version": 1, "pools": [{"name": "seeded", "members": [{"model": "a/x"}]}]}\n',
                    encoding="utf-8",
                )
                with (
                    patch("modules.pools.POOLS_FILE", target),
                    patch("modules.pools.POOLS_EXAMPLE_FILE", example),
                    patch("modules.pools._atomic_publish", side_effect=error("unreadable")),
                ):
                    pools_module.ensure_default_pools_file()

                self.assertFalse(target.exists())
                self.assertNotIn(target.resolve(), _LOADED_DIGESTS)

    def test_release_keeps_the_lock_a_holder_may_still_be_using(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pools.json"
            reserved = reserve_pools_conflict_path(path)
            with _pool_file_lock(reserved):
                pass

            lock_path = reserved.with_name(f".{reserved.name}.lock")
            self.assertTrue(lock_path.exists())
            inode = lock_path.stat().st_ino

            _release_conflict_path(reserved, reservation_digest())

            self.assertFalse(reserved.exists())
            self.assertNotIn(reserved.resolve(), _LOADED_DIGESTS)
            self.assertTrue(lock_path.exists(), "the release unlinked a lock file it may not own")
            self.assertEqual(lock_path.stat().st_ino, inode)
            with _pool_file_lock(reserved):
                self.assertEqual(lock_path.stat().st_ino, inode)

    def test_artifact_predicate_distinguishes_gone_empty_readable_and_unmeasurable(self) -> None:
        for name, prepare, measurable, expected in (
            ("gone", lambda path: None, True, False),
            ("zero-bytes", lambda path: path.touch(), True, False),
            ("body-present",
             lambda path: path.write_text('{"pools": []}\n', encoding="utf-8"), True, True),
            ("unmeasurable", lambda path: path.touch(), False, True),
        ):
            with self.subTest(state=name), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "pools.conflict.json"
                prepare(path)
                if measurable:
                    self.assertIs(conflict_artifact_carries_edits(path), expected)
                else:
                    with patch("modules.pools.os.stat", side_effect=OSError(5, "I/O error")):
                        self.assertIs(conflict_artifact_carries_edits(path), expected)

    def test_artifact_holds_only_the_exact_bytes_that_were_published(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pools.conflict.json"
            body = '{"version": 1, "pools": []}\n'
            digest = pools_module._digest(body)

            for name, prepare, expected in (
                ("gone", lambda: None, False),
                ("empty", lambda: path.touch(), False),
                ("exact-bytes", lambda: path.write_text(body, encoding="utf-8"), True),
                ("different-bytes",
                 lambda: path.write_text('{"version": 1, "pools": []}', encoding="utf-8"), False),
                ("undecodable", lambda: path.write_bytes(b"\xff\xfe\x00"), False),
                ("a-directory", lambda: path.mkdir(), False),
            ):
                with self.subTest(state=name):
                    if path.is_dir():
                        path.rmdir()
                    path.unlink(missing_ok=True)
                    prepare()
                    self.assertIs(conflict_artifact_holds(path, digest), expected)

    def test_release_leaves_bookkeeping_that_belongs_to_another_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pools.json"
            reserved = reserve_pools_conflict_path(path)

            _release_conflict_path(reserved, "a digest nobody published")
            self.assertIn(reserved.resolve(), _LOADED_DIGESTS)
            self.assertTrue(reserved.exists())

            _release_conflict_path(reserved, reservation_digest())
            self.assertNotIn(reserved.resolve(), _LOADED_DIGESTS)
            self.assertFalse(reserved.exists())

    def test_a_cancelled_artifact_unlink_does_not_escape_the_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pools.json"
            reserved = reserve_pools_conflict_path(path)
            real_unlink = os.unlink

            def cancel_artifact_unlink(source, *args, **kwargs):
                if not str(source).endswith(".lock"):
                    raise KeyboardInterrupt
                return real_unlink(source, *args, **kwargs)

            with patch("modules.pools.os.unlink", side_effect=cancel_artifact_unlink):
                try:
                    _release_conflict_path(reserved, reservation_digest())
                except BaseException as escaped:
                    self.fail(f"the cancelled unlink escaped the release as {escaped!r}")

            self.assertNotIn(reserved.resolve(), _LOADED_DIGESTS)
            self.assertTrue(reserved.exists())

    def test_default_publish_rejects_oversized_example(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "pools.json"
            example = root / "pools.example.json"
            example.write_text('{"version": 1, "pools": []}\n' * 20, encoding="utf-8")
            with (
                patch("modules.pools.POOLS_FILE", target),
                patch("modules.pools.POOLS_EXAMPLE_FILE", example),
                patch("modules.pools._MAX_POOL_FILE_BYTES", 32),
                self.assertRaisesRegex(ValueError, "exceeds"),
            ):
                from modules.pools import ensure_default_pools_file

                ensure_default_pools_file()
            self.assertFalse(target.exists())

    def test_interrupted_reservation_leaves_no_orphan(self) -> None:
        real_close = os.close

        def close_then_interrupt(descriptor: int) -> None:
            # os.close cannot be interrupted mid-call, so close-then-raise is the
            # only observable ordering; the unlink after it then fails on a held file.
            real_close(descriptor)
            raise KeyboardInterrupt

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pools.json"
            with patch("modules.pools.os.close", side_effect=close_then_interrupt):
                with self.assertRaises(KeyboardInterrupt):
                    reserve_pools_conflict_path(path)
            self.assertEqual(list(Path(directory).iterdir()), [])
            self.assertEqual(_LOADED_DIGESTS, {})

    @unittest.skipUnless(
        os.name == "posix", "a directory handle can only be opened and fsynced on POSIX"
    )
    def test_publish_flushes_the_parent_directory_after_the_new_name_is_in_place(self) -> None:
        real_fsync = os.fsync
        flushed: list[tuple[int, bool]] = []

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pools.json"

            def note_directory_fsync(file_descriptor: int) -> None:
                status = os.fstat(file_descriptor)
                if stat.S_ISDIR(status.st_mode):
                    flushed.append((status.st_ino, path.exists()))
                return real_fsync(file_descriptor)

            with patch("modules.pools.os.fsync", side_effect=note_directory_fsync):
                save_pools([ModelPool("p", (PoolMember("a/x"),))], path)

            self.assertEqual(flushed, [(os.stat(directory).st_ino, True)])
            self.assertEqual(load_pools(path), [ModelPool("p", (PoolMember("a/x"),))])

    def test_external_json_rejects_invalid_values_at_boundary(self) -> None:
        huge_integer = str(10**1000)
        document = f'''{{
            "version": 1,
            "pools": [
                {{"name": 7, "members": [{{"model": "a/x"}}, {{"model": "b/x"}}]}},
                {{"name": "enabled-string", "enabled": "false", "members": [{{"model": "a/x"}}, {{"model": "b/x"}}]}},
                {{"name": "object-model", "members": [{{"model": {{}}}}, {{"model": "b/x"}}]}},
                {{"name": "cooldown-boolean", "members": [{{"model": "a/x", "cooldown": true}}, {{"model": "b/x"}}]}},
                {{"name": "cooldown-nan", "members": [{{"model": "a/x", "cooldown": NaN}}, {{"model": "b/x"}}]}},
                {{"name": "cooldown-infinity", "members": [{{"model": "a/x", "cooldown": Infinity}}, {{"model": "b/x"}}]}},
                {{"name": "huge-integer", "members": [{{"model": "a/x", "rpm": {huge_integer}}}, {{"model": "b/x"}}]}},
                {{"name": "valid", "members": [{{"model": "a/x"}}, {{"model": "b/x"}}]}}
            ]
        }}'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pools.json"
            path.write_text(document, encoding="utf-8")
            with self.assertLogs("cx.pools", level="WARNING"):
                pools = load_pools(path)

        by_name = {pool.name: pool for pool in pools}
        self.assertEqual(list(by_name), [
            "object-model", "cooldown-boolean", "cooldown-nan",
            "cooldown-infinity", "huge-integer", "valid",
        ])
        self.assertIsNone(by_name["cooldown-boolean"].members[0].cooldown)
        self.assertIsNone(by_name["cooldown-nan"].members[0].cooldown)
        self.assertIsNone(by_name["cooldown-infinity"].members[0].cooldown)
        self.assertIsNone(by_name["huge-integer"].members[0].rpm)

    def test_falsy_non_string_strategy_warns_and_uses_default(self) -> None:
        for value in (None, False, 0, [], {}):
            with self.subTest(value=value):
                document = {
                    "version": 1,
                    "pools": [{
                        "name": "p",
                        "strategy": value,
                        "members": [{"model": "a/x"}, {"model": "b/x"}],
                    }],
                }
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "pools.json"
                    path.write_text(json.dumps(document), encoding="utf-8")
                    with self.assertLogs("cx.pools", level="WARNING") as captured:
                        pools = load_pools(path)

                self.assertEqual(pools[0].strategy, "fill-first")
                self.assertTrue(any("is not a string" in warning for warning in captured.output))

    def test_malformed_member_numbers_warn_and_use_defaults(self) -> None:
        document = {
            "version": 1,
            "pools": [{
                "name": "p",
                "members": [
                    {"model": "a/x", "rpm": "10"},
                    {"model": "b/x", "priority": "0"},
                    {"model": "c/x", "limit": "10"},
                    {"model": "d/x", "cooldown": "1.5"},
                    {"model": "e/x", "rpm": 1.0},
                ],
            }],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pools.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertLogs("cx.pools", level="WARNING") as captured:
                pools = load_pools(path)

        members = {member.model: member for member in pools[0].members}
        self.assertIsNone(members["a/x"].rpm)
        self.assertIsNone(members["b/x"].priority)
        self.assertIsNone(members["c/x"].limit)
        self.assertIsNone(members["d/x"].cooldown)
        self.assertIsNone(members["e/x"].rpm)
        self.assertTrue(any("not an integer" in warning for warning in captured.output))
        self.assertTrue(any("not a number" in warning for warning in captured.output))

    def test_oversized_pool_file_is_rejected_without_recording_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pools.json"
            path.write_text(json.dumps({
                "version": 1,
                "pools": [{"name": "p", "members": [{"model": "a/x"}]}],
            }), encoding="utf-8")
            with patch("modules.pools._MAX_POOL_FILE_BYTES", 32, create=True):
                with self.assertRaisesRegex(RuntimeError, "exceeds"):
                    load_pools(path)
            self.assertNotIn(path, _LOADED_DIGESTS)

    def test_deeply_nested_pool_file_is_rejected_safely(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pools.json"
            path.write_text("[" * 10000 + "0" + "]" * 10000, encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "nest|deep"):
                load_pools(path)
            self.assertNotIn(path, _LOADED_DIGESTS)

    def test_unsupported_version_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pools.json"
            path.write_text(json.dumps({
                "version": 2,
                "pools": [{"name": "p", "members": [{"model": "a/x"}, {"model": "b/x"}]}],
            }), encoding="utf-8")
            with self.assertLogs("cx.pools", level="WARNING"):
                pools = load_pools(path)
        self.assertEqual(pools, [])

    def test_save_and_load_reject_ambiguous_bare_members(self) -> None:
        upstream = [Model("provider/alpha", "provider"), Model("other/alpha", "other")]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pools.json"
            pools = [ModelPool("p", (PoolMember("alpha"), PoolMember("provider/beta")))]
            with self.assertRaisesRegex(RuntimeError, "ambiguous"):
                save_pools(pools, path, upstream_models=upstream)
            self.assertFalse(path.exists())
            path.write_text(json.dumps({
                "version": 1,
                "pools": [{"name": "p", "members": [{"model": "alpha"}, {"model": "provider/beta"}]}],
            }), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "ambiguous"):
                load_pools(path, upstream_models=upstream)
            self.assertNotIn(path, _LOADED_DIGESTS)

    def test_structurally_invalid_document_does_not_authorize_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pools.json"
            original = '{"version": 2, "pools": []}\n'
            path.write_text(original, encoding="utf-8")
            with self.assertLogs("cx.pools", level="WARNING"):
                load_pools(path)
            self.assertNotIn(path, _LOADED_DIGESTS)
            with self.assertRaises(PoolSaveConflictError):
                save_pools([], path)
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_duplicate_json_keys_are_rejected_without_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pools.json"
            original = '{"version": 1, "pools": [], "pools": [{"name": "p", "members": [{"model": "a/x"}, {"model": "b/x"}]}]}\n'
            path.write_text(original, encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "duplicate"):
                load_pools(path)
            self.assertNotIn(path, _LOADED_DIGESTS)
            with self.assertRaises(PoolSaveConflictError):
                save_pools([], path)
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_save_rejects_loader_invalid_values_before_creating_file(self) -> None:
        cases = (
            ModelPool(" ", (PoolMember("a/x"), PoolMember("b/x"))),
            ModelPool("pool", (PoolMember(" "), PoolMember("b/x"))),
            ModelPool("pool", ()),
            ModelPool("pool", (PoolMember("a/x"),), strategy="random"),
            ModelPool("pool", (PoolMember("a/x"),), enabled=1),
            ModelPool("pool", (PoolMember("a/x", rpm=0),)),
            ModelPool("pool", (PoolMember("a/x", priority=-1),)),
            ModelPool("pool", (PoolMember("a/x", limit=0),)),
            ModelPool("pool", (PoolMember("a/x", cooldown=0.0),)),
            ModelPool("pool", (PoolMember("a/x", rpm=2**63),)),
        )
        for pools in cases:
            with self.subTest(pools=pools), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "pools.json"
                with self.assertRaises(ValueError):
                    save_pools([pools], path)
                self.assertFalse(path.exists())

    def test_save_enforces_serialized_body_cap_before_creating_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pools.json"
            with patch("modules.pools._MAX_POOL_FILE_BYTES", 32):
                with self.assertRaisesRegex(ValueError, "exceeds"):
                    save_pools([ModelPool("p", (PoolMember("a/x"),))], path)
            self.assertFalse(path.exists())

    def test_digest_adoption_rejects_structurally_invalid_document(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pools.json"
            path.write_text('{"pools": {}}\n', encoding="utf-8")
            with self.assertRaises(PoolSaveUnreadableError):
                adopt_current_pools_digest(path)
            self.assertNotIn(path, _LOADED_DIGESTS)
            with self.assertRaises(PoolSaveConflictError):
                save_pools([], path)

    def test_two_processes_with_one_digest_publish_one_document(self) -> None:
        context = multiprocessing.get_context()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pools.json"
            save_pools([], path)
            barrier = context.Barrier(2)
            results = context.Queue()
            processes = [
                context.Process(
                    target=_process_save_worker,
                    args=(str(path), pool, barrier, results),
                )
                for pool in (
                    ModelPool("first", (PoolMember("a/first"), PoolMember("b/first"))),
                    ModelPool("second", (PoolMember("a/second"), PoolMember("b/second"))),
                )
            ]
            for process in processes:
                process.start()
            for process in processes:
                process.join(5)
                self.assertEqual(process.exitcode, 0)
            observed = [results.get(timeout=1) for _ in processes]
            self.assertEqual(set(observed), {"saved", "conflict"})
            self.assertIn(load_pools(path), [
                [ModelPool("first", (PoolMember("a/first"), PoolMember("b/first")))],
                [ModelPool("second", (PoolMember("a/second"), PoolMember("b/second")))],
            ])

    def test_concurrent_saves_without_digest_publish_one_complete_document(self) -> None:
        first = [ModelPool("first", (PoolMember("a/first"), PoolMember("b/first")))]
        second = [ModelPool("second", (PoolMember("a/second"), PoolMember("b/second")))]
        barrier = threading.Barrier(2)
        real_fsync = os.fsync

        def slow_fsync(file_descriptor: int) -> None:
            real_fsync(file_descriptor)
            threading.Event().wait(0.05)

        def write(pools: list[ModelPool]) -> None:
            barrier.wait()
            try:
                save_pools(pools, path)
            except PoolSaveConflictError:
                pass

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pools.json"
            with patch("modules.pools.os.fsync", side_effect=slow_fsync):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    list(executor.map(write, (first, second)))
            self.assertIn(load_pools(path), (first, second))

    def test_concurrent_writers_with_one_digest_do_not_overwrite(self) -> None:
        first = ModelPool("first", (PoolMember("a/first"), PoolMember("b/first")))
        second = ModelPool("second", (PoolMember("a/second"), PoolMember("b/second")))
        barrier = threading.Barrier(2)
        real_fsync = os.fsync

        def slow_fsync(file_descriptor: int) -> None:
            real_fsync(file_descriptor)
            threading.Event().wait(0.05)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pools.json"
            path.write_text(json.dumps({"pools": []}), encoding="utf-8")
            load_pools(path)

            def write(pool: ModelPool) -> tuple[str, str]:
                barrier.wait()
                try:
                    save_pools([pool], path)
                    return "saved", pool.name
                except RuntimeError as error:
                    return "conflict", str(error)

            with patch("modules.pools.os.fsync", side_effect=slow_fsync):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    results = list(executor.map(write, (first, second)))

        self.assertEqual({result[0] for result in results}, {"saved", "conflict"})
        saved_name = next(result[1] for result in results if result[0] == "saved")
        self.assertIn(saved_name, {"first", "second"})

    def test_digest_change_before_publish_keeps_external_edit(self) -> None:
        initial = {"pools": []}
        external = {"pools": [{"name": "external", "members": [{"model": "x/y"}]}]}
        started = threading.Event()
        continue_publish = threading.Event()
        real_fsync = os.fsync

        def paused_fsync(file_descriptor: int) -> None:
            real_fsync(file_descriptor)
            started.set()
            self.assertTrue(continue_publish.wait(2))

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pools.json"
            path.write_text(json.dumps(initial), encoding="utf-8")
            load_pools(path)
            replacement = [ModelPool("replacement", (PoolMember("a/new"), PoolMember("b/new")))]
            with patch("modules.pools.os.fsync", side_effect=paused_fsync):
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(save_pools, replacement, path)
                    self.assertTrue(started.wait(2))
                    path.write_text(json.dumps(external), encoding="utf-8")
                    continue_publish.set()
                    with self.assertRaisesRegex(RuntimeError, "changed on disk"):
                        future.result()
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), external)

    def test_priority_zero_and_single_member_pool_load(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pools.json"
            path.write_text(json.dumps({"pools": [{"name": "one", "members": [{"model": "a/x", "priority": 0}]}]}), encoding="utf-8")
            pools = load_pools(path)
        self.assertEqual(pools[0].members[0].priority, 0)
        self.assertEqual(len(pools[0].members), 1)


if __name__ == "__main__":
    unittest.main()
