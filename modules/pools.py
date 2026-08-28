"""Pool configuration loading, validation, and guarded persistence."""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import POOLS_EXAMPLE_FILE, POOLS_FILE
from .models import Model

_LOG = logging.getLogger("cx.pools")
# Content digests, not mtimes: two writes inside one filesystem clock tick
# (~15.6 ms on Windows) share a timestamp, which would hide a lost update.
_LOADED_DIGESTS: dict[Path, str] = {}


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PoolMember:
    model: str
    rpm: int | None = None
    tpm: int | None = None
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


def _integer(value: Any, field: str, *, optional: bool = False, minimum: int = 1) -> int | None:
    if value in (None, "") and optional:
        return None
    if isinstance(value, bool):
        raise RuntimeError(f"Pool {field} must be an integer.")
    try:
        number = int(value)
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"Pool {field} must be an integer.") from error
    if number < minimum:
        comparison = "zero or greater" if minimum == 0 else "greater than zero"
        raise RuntimeError(f"Pool {field} must be {comparison}.")
    return number


def _positive_float(value: Any, field: str, *, optional: bool = False) -> float | None:
    if value in (None, "") and optional:
        return None
    if isinstance(value, bool):
        raise RuntimeError(f"Pool {field} must be a positive number.")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"Pool {field} must be a positive number.") from error
    if number <= 0:
        raise RuntimeError(f"Pool {field} must be greater than zero.")
    return number


def load_pools(path: Path = POOLS_FILE, *, upstream_models: list[Model] | None = None) -> list[ModelPool]:
    """Load pools, accepting the same valid routing schema as the router."""
    if not path.exists():
        _LOADED_DIGESTS[path] = ""
        return []
    try:
        text = path.read_text(encoding="utf-8")
        payload = json.loads(text)
        _LOADED_DIGESTS[path] = _digest(text)
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Could not read pool configuration:\n{path}\n{error}") from error
    raw_pools = payload.get("pools", []) if isinstance(payload, dict) else []
    if not isinstance(raw_pools, list):
        raise RuntimeError("pools.json must contain a top-level 'pools' array.")

    ambiguous_suffixes: set[str] = set()
    if upstream_models is not None:
        owners: dict[str, set[str]] = {}
        for model in upstream_models:
            owners.setdefault(model.id.rsplit("/", 1)[-1], set()).add(model.id)
        ambiguous_suffixes = {suffix for suffix, ids in owners.items() if len(ids) > 1}

    pools: list[ModelPool] = []
    names: set[str] = set()
    for raw_pool in raw_pools:
        if not isinstance(raw_pool, dict):
            raise RuntimeError("Every pool entry must be a JSON object.")
        name = str(raw_pool.get("name", "")).strip()
        if not name:
            raise RuntimeError("Every pool requires a non-empty name.")
        if name in names:
            raise RuntimeError(f"Duplicate pool name: {name}")
        names.add(name)
        strategy = str(raw_pool.get("strategy") or STRATEGIES[0]).strip().lower()
        if strategy not in STRATEGIES:
            raise RuntimeError(
                f"Pool {name!r} strategy must be one of: {', '.join(STRATEGIES)}."
            )
        raw_members = raw_pool.get("members", [])
        if not isinstance(raw_members, list):
            raise RuntimeError(f"Pool {name!r} members must be an array.")
        members: list[PoolMember] = []
        member_names: set[str] = set()
        for raw_member in raw_members:
            if not isinstance(raw_member, dict):
                raise RuntimeError(f"Pool {name!r} has an invalid member.")
            model = str(raw_member.get("model", "")).strip()
            if not model:
                raise RuntimeError(f"Pool {name!r} has a member without a model.")
            if "/" not in model and model in ambiguous_suffixes:
                raise RuntimeError(
                    f"Pool {name!r} member {model!r} is ambiguous: CLIProxyAPI advertises "
                    "more than one model ending in that name. Use its provider-qualified ID."
                )
            if model in member_names:
                raise RuntimeError(f"Pool {name!r} repeats model {model!r}.")
            member_names.add(model)
            members.append(PoolMember(
                model=model,
                rpm=_integer(raw_member.get("rpm"), "RPM", optional=True),
                tpm=_integer(raw_member.get("tpm"), "TPM", optional=True),
                priority=_integer(raw_member.get("priority"), "Priority", optional=True, minimum=0),
                limit=_integer(raw_member.get("limit"), "Limit", optional=True),
                cooldown=_positive_float(raw_member.get("cooldown"), "Cooldown", optional=True),
            ))
        if not members:
            _LOG.warning("Pool %r has no members and will be ignored by the router.", name)
        elif len(members) == 1:
            _LOG.warning("Pool %r has one member; it cannot fail over.", name)
        pools.append(ModelPool(
            name, tuple(members), bool(raw_pool.get("enabled", True)), strategy
        ))
    return pools


def save_pools(pools: list[ModelPool], path: Path = POOLS_FILE) -> None:
    """Atomically save while refusing to overwrite an externally edited file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "pools": [
        {"name": pool.name, "enabled": pool.enabled,
         **({"strategy": pool.strategy} if pool.strategy != STRATEGIES[0] else {}),
         "members": [
            {"model": member.model,
             **({"rpm": member.rpm} if member.rpm is not None else {}),
             **({"tpm": member.tpm} if member.tpm is not None else {}),
             **({"priority": member.priority} if member.priority is not None else {}),
             **({"limit": member.limit} if member.limit is not None else {}),
             **({"cooldown": member.cooldown} if member.cooldown is not None else {})}
            for member in pool.members
        ]}
        for pool in pools
    ]}
    body = json.dumps(payload, indent=2) + "\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(body, encoding="utf-8")
    expected = _LOADED_DIGESTS.get(path)
    actual = _digest(path.read_text(encoding="utf-8")) if path.exists() else ""
    if expected is not None and actual != expected:
        temporary.unlink(missing_ok=True)
        raise RuntimeError("Pool configuration changed on disk; reload before saving so no edits are lost.")
    temporary.replace(path)
    _LOADED_DIGESTS[path] = _digest(body)


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
