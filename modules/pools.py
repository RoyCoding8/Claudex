"""Pool configuration loading, validation, and guarded persistence."""
from __future__ import annotations

import hashlib
import itertools
import json
import logging
import math
import os
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import POOLS_EXAMPLE_FILE, POOLS_FILE
from .models import Model

fcntl_module: Any
try:
    import fcntl
except ImportError:
    fcntl_module = None
else:
    fcntl_module = fcntl

msvcrt_module: Any
try:
    import msvcrt
except ImportError:
    msvcrt_module = None
else:
    msvcrt_module = msvcrt

_LOG = logging.getLogger("cx.pools")
_LOADED_DIGESTS: dict[Path, str] = {}
_SAVE_LOCK = threading.RLock()
_SUPPORTED_VERSION = 1
_MAX_POOL_FILE_BYTES = 1 * 1024 * 1024
_MAX_INTEGER = 2**63 - 1
_MAX_JSON_INTEGER_DIGITS = 19
_INVALID_JSON_INTEGER = object()


class PoolSaveConflictError(RuntimeError):
    pass


class PoolSaveUnreadableError(RuntimeError):
    pass


def _canonical_path(path: Path) -> Path:
    return path.resolve(strict=False)


@contextmanager
def _pool_file_lock(path: Path) -> Iterator[None]:
    path = _canonical_path(path)
    lock_path = path.with_name(f".{path.name}.lock")
    with _SAVE_LOCK:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as handle:
            if fcntl_module is not None:
                fcntl_module.flock(handle.fileno(), fcntl_module.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl_module.flock(handle.fileno(), fcntl_module.LOCK_UN)
            elif msvcrt_module is not None:
                # No primer byte: msvcrt refuses a read of a locked range, so
                # priming made an ordinary wait surface as PermissionError.
                handle.seek(0)
                msvcrt_module.locking(handle.fileno(), msvcrt_module.LK_LOCK, 1)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt_module.locking(handle.fileno(), msvcrt_module.LK_UNLCK, 1)
            else:
                raise RuntimeError("Pool persistence requires an interprocess file-lock primitive.")


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_pool_text(path: Path) -> str:
    with path.open("rb") as handle:
        data = handle.read(_MAX_POOL_FILE_BYTES + 1)
    if len(data) > _MAX_POOL_FILE_BYTES:
        raise ValueError(f"pool configuration exceeds {_MAX_POOL_FILE_BYTES} bytes")
    return data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


@dataclass(frozen=True, slots=True)
class PoolMember:
    model: str
    rpm: int | None = None
    priority: int | None = None
    limit: int | None = None
    cooldown: float | None = None


STRATEGIES = ("fill-first", "round-robin", "weighted", "least-busy")


@dataclass(frozen=True, slots=True)
class ModelPool:
    name: str
    members: tuple[PoolMember, ...]
    enabled: bool = True
    strategy: str = STRATEGIES[0]


class PoolSaveRecoveryError(RuntimeError):
    def __init__(self, pools: list[ModelPool], message: str) -> None:
        super().__init__(message)
        self.pools = tuple(pools)


@dataclass(frozen=True, slots=True)
class _LoadedPoolDocument:
    pools: tuple[ModelPool, ...]
    warnings: tuple[str, ...]
    digest: str | None


def _parse_json_integer(value: str) -> int | object:
    if len(value.lstrip("-")) > _MAX_JSON_INTEGER_DIGITS:
        return _INVALID_JSON_INTEGER
    return int(value)


def _safe_repr(value: Any) -> str:
    try:
        return repr(value)
    except (TypeError, ValueError):
        return type(value).__name__


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _is_structurally_valid(payload: Any) -> bool:
    return (
        isinstance(payload, dict)
        and type(payload.get("version", _SUPPORTED_VERSION)) is int
        and payload.get("version", _SUPPORTED_VERSION) == _SUPPORTED_VERSION
        and isinstance(payload.get("pools", []), list)
    )


def _lenient_int(value: Any, field: str, name: str, warnings: list[str],
                 *, minimum: int = 1) -> int | None:
    if value in (None, ""):
        return None
    if type(value) is not int:
        warnings.append(f"Pool {name!r} {field} {_safe_repr(value)} is not an integer; using the default.")
        return None
    if value < minimum:
        warnings.append(f"Pool {name!r} {field} must be at least {minimum}; using the default.")
        return None
    if value > _MAX_INTEGER:
        warnings.append(f"Pool {name!r} {field} is too large; using the default.")
        return None
    return value


def _lenient_float(value: Any, field: str, name: str, warnings: list[str]) -> float | None:
    if value in (None, ""):
        return None
    if type(value) not in (int, float):
        warnings.append(f"Pool {name!r} {field} {_safe_repr(value)} is not a number; using the default.")
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        warnings.append(f"Pool {name!r} {field} {_safe_repr(value)} is not a number; using the default.")
        return None
    if not math.isfinite(number):
        warnings.append(f"Pool {name!r} {field} must be finite; using the default.")
        return None
    if number <= 0:
        warnings.append(f"Pool {name!r} {field} must be greater than zero; using the default.")
        return None
    return number


def parse_pool_document(payload: Any) -> tuple[list[ModelPool], list[str]]:
    """Structural parse shared by the launcher and the live router."""
    warnings: list[str] = []
    pools: list[ModelPool] = []
    if not isinstance(payload, dict):
        return pools, ["pools.json must contain a top-level JSON object; ignoring it."]
    version = payload.get("version", _SUPPORTED_VERSION)
    if type(version) is not int or version != _SUPPORTED_VERSION:
        return pools, [f"pools.json version must be {_SUPPORTED_VERSION}; ignoring it."]
    raw_pools = payload.get("pools", [])
    if not isinstance(raw_pools, list):
        return pools, ["pools.json 'pools' must be an array; ignoring it."]
    names: set[str] = set()
    for raw_pool in raw_pools:
        if not isinstance(raw_pool, dict):
            warnings.append("Skipping a pool entry that is not a JSON object.")
            continue
        name_value = raw_pool.get("name")
        if not isinstance(name_value, str):
            warnings.append("Skipping a pool with a non-string name.")
            continue
        name = name_value.strip()
        if not name:
            warnings.append("Skipping a pool without a name.")
            continue
        if name in names:
            warnings.append(f"Duplicate pool name {name!r}; keeping the first.")
            continue
        strategy_value = raw_pool.get("strategy", STRATEGIES[0])
        if not isinstance(strategy_value, str):
            warnings.append(
                f"Pool {name!r} strategy {_safe_repr(strategy_value)} is not a string; "
                f"using {STRATEGIES[0]}."
            )
            strategy = STRATEGIES[0]
        else:
            strategy = strategy_value.strip().lower()
        if strategy not in STRATEGIES:
            warnings.append(
                f"Pool {name!r} strategy {raw_pool.get('strategy')!r} is not one of "
                f"{', '.join(STRATEGIES)}; using {STRATEGIES[0]}.")
            strategy = STRATEGIES[0]
        enabled = raw_pool.get("enabled", True)
        if type(enabled) is not bool:
            warnings.append(f"Pool {name!r} enabled must be a JSON boolean; skipping the pool.")
            continue
        raw_members = raw_pool.get("members", [])
        if not isinstance(raw_members, list):
            warnings.append(f"Pool {name!r} members must be an array; skipping the pool.")
            continue
        members: list[PoolMember] = []
        member_names: set[str] = set()
        for raw_member in raw_members:
            if not isinstance(raw_member, dict):
                warnings.append(f"Pool {name!r} has an invalid member entry; skipping it.")
                continue
            model_value = raw_member.get("model")
            if not isinstance(model_value, str):
                warnings.append(f"Pool {name!r} has a member with a non-string model; skipping it.")
                continue
            model = model_value.strip()
            if not model:
                warnings.append(f"Pool {name!r} has a member without a model; skipping it.")
                continue
            if model in member_names:
                warnings.append(f"Pool {name!r} repeats model {model!r}; keeping the first.")
                continue
            member_names.add(model)
            rpm = _lenient_int(raw_member.get("rpm"), "rpm", name, warnings)
            members.append(PoolMember(
                model=model,
                rpm=rpm,
                priority=_lenient_int(raw_member.get("priority"), "priority", name, warnings, minimum=0),
                limit=_lenient_int(raw_member.get("limit"), "limit", name, warnings),
                cooldown=_lenient_float(raw_member.get("cooldown"), "cooldown", name, warnings),
            ))
        if not members:
            warnings.append(f"Pool {name!r} has no usable members; skipping the pool.")
            continue
        if len(members) == 1:
            warnings.append(f"Pool {name!r} has one member; it cannot fail over.")
        names.add(name)
        pools.append(ModelPool(name, tuple(members), enabled, strategy))
    return pools, warnings


def _load_pool_file(path: Path) -> _LoadedPoolDocument:
    if not path.exists():
        return _LoadedPoolDocument((), (), "")
    try:
        text = _read_pool_text(path)
        payload = json.loads(text, parse_int=_parse_json_integer, object_pairs_hook=_reject_duplicate_json_keys)
    except RecursionError as error:
        raise RuntimeError(f"Could not read pool configuration:\n{path}\nJSON nesting is too deep.") from error
    except (OSError, ValueError, UnicodeDecodeError) as error:
        raise RuntimeError(f"Could not read pool configuration:\n{path}\n{error}") from error
    pools, warnings = parse_pool_document(payload)
    digest = _digest(text) if _is_structurally_valid(payload) else None
    return _LoadedPoolDocument(tuple(pools), tuple(warnings), digest)


def load_pools(path: Path = POOLS_FILE, *, upstream_models: list[Model] | None = None) -> list[ModelPool]:
    """Load pools, logging a warning per entry the document itself got wrong; an unreadable document, a duplicate JSON key, an ambiguous member, or a pool name that collides with an advertised model ID raises when upstream_models is supplied. Structurally invalid documents do not authorize a save.

    Validating against upstream models raises but does not report the mismatches it
    finds: a caller that goes on to use the pools owns telling the user about them,
    and a load that reported them here would report them again for the same launch.
    """
    path = _canonical_path(path)
    with _pool_file_lock(path):
        try:
            loaded = _load_pool_file(path)
            if loaded.digest is not None and upstream_models is not None:
                validate_pools_against_models(list(loaded.pools), upstream_models)
        except Exception:
            _LOADED_DIGESTS.pop(path, None)
            raise
        for warning in loaded.warnings:
            _LOG.warning("pools.json: %s", warning)
        if loaded.digest is None:
            _LOADED_DIGESTS.pop(path, None)
            return list(loaded.pools)
        _LOADED_DIGESTS[path] = loaded.digest
        return list(loaded.pools)


def _reject_ambiguous_members(pools: list[ModelPool], upstream_models: list[Model]) -> None:
    owners: dict[str, set[str]] = {}
    for upstream_model in upstream_models:
        owners.setdefault(upstream_model.id.rsplit("/", 1)[-1], set()).add(upstream_model.id)
    ambiguous = {suffix for suffix, ids in owners.items() if len(ids) > 1}
    for pool in pools:
        for member in pool.members:
            member_model = member.model.strip()
            if "/" not in member_model and member_model in ambiguous:
                raise RuntimeError(
                    f"Pool {pool.name!r} member {member_model!r} is ambiguous: CLIProxyAPI advertises "
                    "more than one model ending in that name. Use its provider-qualified ID."
                )


def _present(**fields: Any) -> dict[str, Any]:
    return {key: value for key, value in fields.items() if value is not None}


def _validate_optional_int(value: Any, field: str, name: str, minimum: int) -> None:
    if value is None:
        return
    if type(value) is not int:
        raise ValueError(f"Pool {name!r} {field} must be an integer.")
    if value < minimum or value > _MAX_INTEGER:
        raise ValueError(f"Pool {name!r} {field} must be between {minimum} and {_MAX_INTEGER}.")


def _validate_pools_for_save(pools: list[ModelPool]) -> None:
    names: set[str] = set()
    for pool in pools:
        if not isinstance(pool.name, str) or not pool.name.strip():
            raise ValueError("Pool name must not be blank.")
        if type(pool.enabled) is not bool:
            raise ValueError(f"Pool {pool.name!r} enabled must be a boolean.")
        if not isinstance(pool.strategy, str) or pool.strategy not in STRATEGIES:
            raise ValueError(f"Pool {pool.name!r} strategy must be one of {', '.join(STRATEGIES)}.")
        name = pool.name.strip()
        if name in names:
            raise ValueError(f"Duplicate pool name {name!r}.")
        names.add(name)
        if not isinstance(pool.members, (tuple, list)) or not pool.members:
            raise ValueError(f"Pool {name!r} must have at least one member.")
        members: set[str] = set()
        for member in pool.members:
            if not isinstance(member, PoolMember):
                raise ValueError(f"Pool {name!r} has an invalid member.")
            if not isinstance(member.model, str) or not member.model.strip():
                raise ValueError(f"Pool {name!r} member model must not be blank.")
            model = member.model.strip()
            if model in members:
                raise ValueError(f"Duplicate member {model!r} in pool {name!r}.")
            members.add(model)
            _validate_optional_int(member.rpm, "rpm", name, 1)
            _validate_optional_int(member.priority, "priority", name, 0)
            _validate_optional_int(member.limit, "limit", name, 1)
            if member.cooldown is not None:
                if type(member.cooldown) not in (int, float):
                    raise ValueError(f"Pool {name!r} cooldown must be a number.")
                try:
                    cooldown = float(member.cooldown)
                except (TypeError, ValueError, OverflowError) as error:
                    raise ValueError(f"Pool {name!r} cooldown must be a number.") from error
                if not math.isfinite(cooldown) or cooldown <= 0:
                    raise ValueError(f"Pool {name!r} cooldown must be finite and greater than zero.")


def _read_current_digest(path: Path) -> tuple[bool, str]:
    try:
        if not path.exists():
            return False, ""
        return True, _digest(_read_pool_text(path))
    except (OSError, ValueError, UnicodeDecodeError) as error:
        raise PoolSaveUnreadableError(
            f"Pool configuration at {path} is unreadable; reload before saving.\n{error}"
        ) from error


def _fsync_directory(directory: Path) -> None:
    """The file's own fsync does not flush the directory entry a rename creates."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _body_landed(path: Path, body: str) -> bool:
    """Whether the bytes on disk are exactly the body a cancelled publish was writing.

    The only trustworthy witness to a publish that was interrupted partway is
    the file itself: the rename is what publishes, and a cancel that landed
    before it leaves the previous contents in place.
    """
    exists, actual = _read_current_digest(path)
    return exists and actual == _digest(body)


def _atomic_publish(
    path: Path,
    body: str,
    expected_digest: str | None = None,
    *,
    expected_missing: bool = False,
) -> None:
    temporary: str | None = None
    descriptor: int | None = None
    replaced = False
    try:
        descriptor, temporary = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp",
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        if expected_missing or expected_digest is not None:
            exists, actual = _read_current_digest(path)
            if (expected_missing and exists) or (
                expected_digest is not None and actual != expected_digest
            ):
                raise PoolSaveConflictError(
                    "Pool configuration changed on disk; reload before saving so no edits are lost."
                )
        try:
            Path(temporary).replace(path)
            replaced = True
            _fsync_directory(path.parent)
        except KeyboardInterrupt:
            if not _body_landed(path, body):
                raise
            replaced = True
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            try:
                Path(temporary).unlink(missing_ok=True)
            except BaseException:
                if not replaced:
                    raise


def save_pools(
    pools: list[ModelPool],
    path: Path = POOLS_FILE,
    *,
    upstream_models: list[Model] | None = None,
) -> str:
    """Atomically publish a save with best-effort conflict detection. Returns the digest now on disk."""
    _validate_pools_for_save(pools)
    if upstream_models is not None:
        validate_pools_against_models(pools, upstream_models)
    payload = {"version": _SUPPORTED_VERSION, "pools": [
        {"name": pool.name.strip(), "enabled": pool.enabled,
         **({"strategy": pool.strategy} if pool.strategy != STRATEGIES[0] else {}),
         "members": [
            {"model": member.model.strip(),
             **_present(rpm=member.rpm, priority=member.priority,
                        limit=member.limit, cooldown=member.cooldown)}
            for member in pool.members
        ]}
        for pool in pools
    ]}
    body = json.dumps(payload, indent=2, allow_nan=False) + "\n"
    if len(body.encode("utf-8")) > _MAX_POOL_FILE_BYTES:
        raise ValueError(f"serialized pool configuration exceeds {_MAX_POOL_FILE_BYTES} bytes")
    path = _canonical_path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        expected_before_lock = _LOADED_DIGESTS.get(path)
        with _pool_file_lock(path):
            _atomic_publish(
                path,
                body,
                expected_before_lock,
                expected_missing=expected_before_lock is None,
            )
            published = _digest(body)
            _LOADED_DIGESTS[path] = published
    except KeyboardInterrupt:
        if not _body_landed(path, body):
            raise
        published = _digest(body)
        _LOADED_DIGESTS[path] = published
    except OSError as error:
        raise PoolSaveUnreadableError(
            f"Pool configuration at {path} could not be published; your edits are still in memory.\n{error}"
        ) from error
    return published


def adopt_current_pools_digest(path: Path = POOLS_FILE) -> None:
    """Treat a structurally valid on-disk file as authoritative, or a missing file as empty, so the next save can proceed."""
    path = _canonical_path(path)
    with _pool_file_lock(path):
        _LOADED_DIGESTS.pop(path, None)
        if not path.exists():
            _LOADED_DIGESTS[path] = ""
            return
        try:
            text = _read_pool_text(path)
            payload = json.loads(text, parse_int=_parse_json_integer, object_pairs_hook=_reject_duplicate_json_keys)
            if not _is_structurally_valid(payload):
                raise ValueError("pool configuration has an invalid document structure")
        except (OSError, ValueError, UnicodeDecodeError) as error:
            raise PoolSaveUnreadableError(
                f"Pool configuration at {path} cannot be adopted; reload before saving.\n{error}"
            ) from error
        _LOADED_DIGESTS[path] = _digest(text)


def _conflict_artifact_names(path: Path) -> Iterator[Path]:
    for index in itertools.count():
        suffix = "" if index == 0 else f".{index}"
        yield path.with_name(f"{path.stem}.conflict{suffix}.json")


def reserve_pools_conflict_path(path: Path = POOLS_FILE) -> Path:
    path = _canonical_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    names = _conflict_artifact_names(path)
    while True:
        candidate = next(names)
        try:
            descriptor = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        try:
            os.close(descriptor)
            with _SAVE_LOCK:
                _LOADED_DIGESTS[candidate] = reservation_digest()
        except BaseException:
            candidate.unlink(missing_ok=True)
            raise
        return candidate


def reservation_digest() -> str:
    """The digest a conflict artifact carries between reservation and publication, when it holds no bytes."""
    return _digest("")


def _release_conflict_path(path: Path, digest: str) -> None:
    """Reclaim a conflict artifact's bookkeeping, and the artifact only while it
    still holds nothing.

    The reservation is private and unique, so nothing else can address this
    artifact and its cleanup needs no guard beyond swallowing an interruption.
    Whether the pending edits are already in it is a fact about its bytes, not
    about the digest a partial publish can leave behind; a caller that has just
    published must still confirm them with conflict_artifact_holds.

    The lock file is deliberately left in place: flock lives on the inode, so
    unlinking it while another process may hold it lets the next opener create a
    fresh inode and flock that, destroying mutual exclusion.
    """
    path = _canonical_path(path)
    with _SAVE_LOCK:
        reserved = _LOADED_DIGESTS.get(path) == digest
        if reserved:
            _LOADED_DIGESTS.pop(path, None)
    if reserved and not conflict_artifact_carries_edits(path):
        try:
            path.unlink(missing_ok=True)
        except BaseException:
            pass


def conflict_artifact_holds(path: Path, digest: str) -> bool:
    """Whether the artifact on disk still carries exactly the bytes that were published to it."""
    try:
        exists, actual = _read_current_digest(_canonical_path(path))
    except PoolSaveUnreadableError:
        return False
    return exists and actual == digest


def conflict_artifact_carries_edits(path: Path) -> bool:
    """Whether a conflict artifact can still hold the pending edits.

    The publish renames a temporary file that already carried the whole body, so
    any bytes at all mean the edits are there and nothing may reclaim it. An
    artifact that cannot be measured is treated as holding them.
    """
    try:
        return _canonical_path(path).stat().st_size > 0
    except FileNotFoundError:
        return False
    except OSError:
        return True


def pool_names(pools: list[ModelPool] | None = None) -> set[str]:
    current = pools if pools is not None else load_pools()
    return {pool.name for pool in current if pool.enabled and pool.members}


def validate_pools_against_models(pools: list[ModelPool], upstream_models: list[Model]) -> list[str]:
    _reject_ambiguous_members(pools, upstream_models)
    upstream_ids = {model.id for model in upstream_models}
    warnings: list[str] = []
    for pool in pools:
        name = pool.name.strip()
        if name in upstream_ids:
            raise RuntimeError(f"Pool {name!r} conflicts with an existing CLIProxyAPI model ID.")
        missing = [member.model.strip() for member in pool.members if member.model.strip() not in upstream_ids]
        if missing:
            warnings.append(f"Pool {name!r} references models not currently advertised by CLIProxyAPI: {', '.join(missing)}")
    return warnings


def ensure_default_pools_file(path: Path | None = None) -> None:
    path = _canonical_path(POOLS_FILE if path is None else path)
    with _pool_file_lock(path):
        if path.exists():
            return
        if POOLS_EXAMPLE_FILE.is_file():
            try:
                text = _read_pool_text(POOLS_EXAMPLE_FILE)
                payload = json.loads(text, parse_int=_parse_json_integer, object_pairs_hook=_reject_duplicate_json_keys)
                if not _is_structurally_valid(payload):
                    raise ValueError("pool configuration has an invalid document structure")
            except (OSError, ValueError, UnicodeDecodeError, RecursionError) as error:
                raise ValueError(
                    f"Pool configuration at {POOLS_EXAMPLE_FILE} cannot be published.\n{error}"
                ) from error
        else:
            text = json.dumps({"version": _SUPPORTED_VERSION, "pools": []}, indent=2) + "\n"
        try:
            _atomic_publish(path, text, expected_missing=True)
        except (PoolSaveConflictError, PoolSaveUnreadableError):
            return
        _LOADED_DIGESTS[path] = _digest(text)
