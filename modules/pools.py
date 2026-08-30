"""Pool configuration loading, validation, and guarded persistence."""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import POOLS_EXAMPLE_FILE, POOLS_FILE
from .models import Model

_LOG = logging.getLogger("cx.pools")
_LOADED_DIGESTS: dict[Path, str] = {}


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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


def _lenient_int(value: Any, field: str, name: str, warnings: list[str],
                 *, minimum: int = 1) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
        warnings.append(f"Pool {name!r} {field} {value!r} is not a whole number; using the default.")
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        warnings.append(f"Pool {name!r} {field} {value!r} is not an integer; using the default.")
        return None
    if number < minimum:
        warnings.append(f"Pool {name!r} {field} must be at least {minimum}; using the default.")
        return None
    return number


def _lenient_float(value: Any, field: str, name: str, warnings: list[str]) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        warnings.append(f"Pool {name!r} {field} {value!r} is not a number; using the default.")
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
    raw_pools = payload.get("pools", [])
    if not isinstance(raw_pools, list):
        return pools, ["pools.json 'pools' must be an array; ignoring it."]
    names: set[str] = set()
    for raw_pool in raw_pools:
        if not isinstance(raw_pool, dict):
            warnings.append("Skipping a pool entry that is not a JSON object.")
            continue
        name = str(raw_pool.get("name", "")).strip()
        if not name:
            warnings.append("Skipping a pool without a name.")
            continue
        if name in names:
            warnings.append(f"Duplicate pool name {name!r}; keeping the first.")
            continue
        strategy = str(raw_pool.get("strategy") or STRATEGIES[0]).strip().lower()
        if strategy not in STRATEGIES:
            warnings.append(
                f"Pool {name!r} strategy {raw_pool.get('strategy')!r} is not one of "
                f"{', '.join(STRATEGIES)}; using {STRATEGIES[0]}.")
            strategy = STRATEGIES[0]
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
            model = str(raw_member.get("model", "")).strip()
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
        pools.append(ModelPool(name, tuple(members), bool(raw_pool.get("enabled", True)), strategy))
    return pools, warnings


def load_pools(path: Path = POOLS_FILE, *, upstream_models: list[Model] | None = None) -> list[ModelPool]:
    """Load pools; unusable entries are skipped with a warning, never a crash."""
    if not path.exists():
        _LOADED_DIGESTS[path] = ""
        return []
    try:
        text = path.read_text(encoding="utf-8")
        payload = json.loads(text)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise RuntimeError(f"Could not read pool configuration:\n{path}\n{error}") from error
    pools, warnings = parse_pool_document(payload)
    for warning in warnings:
        _LOG.warning("pools.json: %s", warning)
    if upstream_models is not None:
        _reject_ambiguous_members(pools, upstream_models)
    _LOADED_DIGESTS[path] = _digest(text)
    return pools


def _reject_ambiguous_members(pools: list[ModelPool], upstream_models: list[Model]) -> None:
    owners: dict[str, set[str]] = {}
    for model in upstream_models:
        owners.setdefault(model.id.rsplit("/", 1)[-1], set()).add(model.id)
    ambiguous = {suffix for suffix, ids in owners.items() if len(ids) > 1}
    for pool in pools:
        for member in pool.members:
            if "/" not in member.model and member.model in ambiguous:
                raise RuntimeError(
                    f"Pool {pool.name!r} member {member.model!r} is ambiguous: CLIProxyAPI advertises "
                    "more than one model ending in that name. Use its provider-qualified ID."
                )


def _present(**fields: Any) -> dict[str, Any]:
    return {key: value for key, value in fields.items() if value is not None}


def save_pools(pools: list[ModelPool], path: Path = POOLS_FILE) -> None:
    """Atomically save while refusing to overwrite an externally edited file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "pools": [
        {"name": pool.name, "enabled": pool.enabled,
         **({"strategy": pool.strategy} if pool.strategy != STRATEGIES[0] else {}),
         "members": [
            {"model": member.model,
             **_present(rpm=member.rpm, priority=member.priority,
                        limit=member.limit, cooldown=member.cooldown)}
            for member in pool.members
        ]}
        for pool in pools
    ]}
    body = json.dumps(payload, indent=2) + "\n"
    expected = _LOADED_DIGESTS.get(path)
    try:
        actual = _digest(path.read_text(encoding="utf-8")) if path.exists() else ""
    except (OSError, UnicodeDecodeError) as error:
        raise RuntimeError(f"Pool configuration at {path} is unreadable; reload before saving.\n{error}") from error
    if expected is not None and actual != expected:
        raise RuntimeError("Pool configuration changed on disk; reload before saving so no edits are lost.")
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    _LOADED_DIGESTS[path] = _digest(body)


def adopt_current_pools_digest(path: Path = POOLS_FILE) -> None:
    """Treat the on-disk file as authoritative so the next save can proceed."""
    try:
        _LOADED_DIGESTS[path] = _digest(path.read_text(encoding="utf-8")) if path.exists() else ""
    except (OSError, UnicodeDecodeError):
        _LOADED_DIGESTS[path] = ""


def pool_names(pools: list[ModelPool] | None = None) -> set[str]:
    current = pools if pools is not None else load_pools()
    return {pool.name for pool in current if pool.enabled and pool.members}


def validate_pools_against_models(pools: list[ModelPool], upstream_models: list[Model]) -> list[str]:
    upstream_ids = {model.id for model in upstream_models}
    warnings: list[str] = []
    for pool in pools:
        if pool.name in upstream_ids:
            raise RuntimeError(f"Pool {pool.name!r} conflicts with an existing CLIProxyAPI model ID.")
        missing = [member.model for member in pool.members if member.model not in upstream_ids]
        if missing:
            warnings.append(f"Pool {pool.name!r} references models not currently advertised by CLIProxyAPI: {', '.join(missing)}")
    return warnings


def ensure_default_pools_file() -> None:
    if POOLS_FILE.exists():
        return
    if POOLS_EXAMPLE_FILE.is_file():
        POOLS_FILE.parent.mkdir(parents=True, exist_ok=True)
        POOLS_FILE.write_bytes(POOLS_EXAMPLE_FILE.read_bytes())
    else:
        save_pools([])
