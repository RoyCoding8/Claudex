"""Pool configuration: load and save ``data/pools.json``.

Pools describe how one client-facing model name expands into a list of real
CLIProxyAPI models. The router (``modules.router``) reads this file live via
mtime hot-reload; changing it in the F7 UI takes effect on the next request
without restarting anything.

The schema is a stable JSON document — it does NOT depend on the routing
engine underneath. When we replaced LiteLLM with the in-house router,
``pools.json`` stayed the same, and this module dropped the (previously
present) LiteLLM YAML generator.

Schema (data/pools.json)::

    {
      "version": 1,
      "pools": [
        {
          "name": "Opus-level",
          "enabled": true,
          "members": [
            {"model": "nvidia_nim/z-ai/glm-5.2", "rpm": 40, "priority": 1},
            {"model": "@cf/zai-org/glm-5.2",     "rpm": 30, "priority": 2}
          ]
        }
      ]
    }

Semantics understood by the router:

    * ``priority`` — lower is tried first. Missing → treated as 0.
    * ``rpm`` — weight for random selection *within a priority tier*.
      Missing → treated as 1.
    * ``tpm`` — parsed and preserved for round-tripping, currently unused
      by the router (it does not enforce token budgets).
    * ``enabled: false`` pools are ignored by the router and hidden from
      ``/v1/models``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import POOLS_EXAMPLE_FILE, POOLS_FILE
from .models import Model


@dataclass(frozen=True, slots=True)
class PoolMember:
    model: str
    rpm: int | None = None
    tpm: int | None = None
    priority: int | None = None


@dataclass(frozen=True, slots=True)
class ModelPool:
    name: str
    members: tuple[PoolMember, ...]
    enabled: bool = True


def _positive_int(value: Any, field: str, *, optional: bool = False) -> int | None:
    if value in (None, "") and optional:
        return None
    if isinstance(value, bool):
        raise RuntimeError(f"Pool {field} must be a positive integer.")
    try:
        number = int(value)
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"Pool {field} must be a positive integer.") from error
    if number <= 0:
        raise RuntimeError(f"Pool {field} must be greater than zero.")
    return number


def load_pools(
    path: Path = POOLS_FILE,
    *,
    upstream_models: list[Model] | None = None,
) -> list[ModelPool]:
    """Load ``pools.json``. Raises ``RuntimeError`` if the file is malformed.

    ``upstream_models`` is optional and used only to detect ambiguous
    unqualified model IDs (e.g. bare ``glm-5.2`` when CLIProxyAPI serves both
    ``ollama/glm-5.2`` and ``nvidia_nim/z-ai/glm-5.2``). The router routes
    on the exact string, so an ambiguous entry would silently hit the wrong
    provider — we surface it as an error here instead.
    """
    if not path.exists():
        return []

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Could not read pool configuration:\n{path}\n{error}") from error

    raw_pools = payload.get("pools", []) if isinstance(payload, dict) else []
    if not isinstance(raw_pools, list):
        raise RuntimeError("pools.json must contain a top-level 'pools' array.")

    ambiguous_suffixes: set[str] = set()
    if upstream_models is not None:
        suffix_owners: dict[str, set[str]] = {}
        for model in upstream_models:
            suffix = model.id.rsplit("/", 1)[-1]
            suffix_owners.setdefault(suffix, set()).add(model.id)
        ambiguous_suffixes = {
            suffix for suffix, owners in suffix_owners.items() if len(owners) > 1
        }

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
                    f"Pool {name!r} member {model!r} is ambiguous: CLIProxyAPI "
                    "advertises more than one model ending in that name (e.g. "
                    "from different providers such as NIM vs Ollama). Use the "
                    "provider-qualified id it advertises, such as "
                    f"'nvidia_nim/{model}' or 'ollama/{model}'."
                )
            if model in member_names:
                raise RuntimeError(f"Pool {name!r} repeats model {model!r}.")
            member_names.add(model)
            rpm = _positive_int(raw_member.get("rpm"), "RPM", optional=True)
            tpm = _positive_int(raw_member.get("tpm"), "TPM", optional=True)
            priority = _positive_int(raw_member.get("priority"), "Priority", optional=True)
            members.append(PoolMember(model=model, rpm=rpm, tpm=tpm, priority=priority))

        # The router tolerates single-member pools (they just do failover-to-nowhere),
        # but the UI never creates one and single-member pools are almost always a
        # config mistake, so we still surface it as an error at save/load time.
        if len(members) < 2:
            raise RuntimeError(f"Pool {name!r} needs at least two provider models.")

        pools.append(
            ModelPool(
                name=name,
                members=tuple(members),
                enabled=bool(raw_pool.get("enabled", True)),
            )
        )

    return pools


def save_pools(pools: list[ModelPool], path: Path = POOLS_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "pools": [
            {
                "name": pool.name,
                "enabled": pool.enabled,
                "members": [
                    {
                        "model": member.model,
                        **({"rpm": member.rpm} if member.rpm else {}),
                        **({"tpm": member.tpm} if member.tpm else {}),
                        **({"priority": member.priority} if member.priority else {}),
                    }
                    for member in pool.members
                ],
            }
            for pool in pools
        ],
    }
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def pool_names(pools: list[ModelPool] | None = None) -> set[str]:
    current = pools if pools is not None else load_pools()
    return {pool.name for pool in current if pool.enabled}


def validate_pools_against_models(
    pools: list[ModelPool],
    upstream_models: list[Model],
) -> list[str]:
    """Return non-fatal warnings for pool members that CLIProxyAPI doesn't advertise.

    Raises for pool names that collide with a real CLIProxyAPI model id, since
    the router would never surface such a name to the client.
    """
    upstream_ids = {model.id for model in upstream_models}
    warnings: list[str] = []
    for pool in pools:
        if pool.name in upstream_ids:
            raise RuntimeError(
                f"Pool {pool.name!r} conflicts with an existing CLIProxyAPI model ID."
            )
        missing = [m.model for m in pool.members if m.model not in upstream_ids]
        if missing:
            warnings.append(
                f"Pool {pool.name!r} references models not currently advertised by "
                f"CLIProxyAPI: {', '.join(missing)}"
            )
    return warnings


def ensure_default_pools_file() -> None:
    if POOLS_FILE.exists():
        return
    if POOLS_EXAMPLE_FILE.is_file():
        POOLS_FILE.parent.mkdir(parents=True, exist_ok=True)
        POOLS_FILE.write_bytes(POOLS_EXAMPLE_FILE.read_bytes())
        return
    save_pools([])
