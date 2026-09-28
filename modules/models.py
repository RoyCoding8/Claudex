"""Model discovery and categorisation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from http.client import HTTPException
from typing import TypeGuard
from urllib.error import HTTPError, URLError
from urllib.request import Request

from .config import PROXY_API_KEY, PROXY_HOST, PROXY_PORT, ROUTER_API_KEY, ROUTER_HOST, ROUTER_PORT
from .urls import http_url
from .urls import open_http_url as _open_url


@dataclass(frozen=True, slots=True)
class Model:
    id: str
    owner: str
    is_pool: bool = False


CATEGORIES = ("All", "Pools", "Codex", "Grok", "Kimi", "Custom")

_MAX_MODELS_BYTES = 32 * 1024 * 1024
_MAX_MODEL_ID_LENGTH = 256


def _valid_model_text(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and len(value) <= _MAX_MODEL_ID_LENGTH and value.isprintable()


def _model_records(body: bytes) -> list[tuple[str, str]]:
    payload = json.loads(body.decode("utf-8"))
    if (
        not isinstance(payload, dict)
        or payload.get("object") != "list"
        or not isinstance(payload.get("data"), list)
    ):
        raise ValueError("invalid model response")
    records: list[tuple[str, str]] = []
    model_ids: set[str] = set()
    for item in payload["data"]:
        if not isinstance(item, dict):
            raise ValueError("invalid model record")
        model_id = item.get("id")
        owner = item.get("owned_by", "")
        if not _valid_model_text(model_id) or not model_id.strip() or not _valid_model_text(owner):
            raise ValueError("invalid model record")
        normalized = model_id.strip()
        if normalized in model_ids:
            raise ValueError(f"duplicate model id: {normalized}")
        model_ids.add(normalized)
        records.append((normalized, owner))
    return records


def _models_url(host: str, port: int) -> str:
    return http_url(host, port, "/v1/models")


def fetch_models_from(
    host: str,
    port: int,
    api_key: str,
    service_name: str,
    timeout: float = 5.0,
    pool_names: set[str] | None = None,
    owner_overrides: dict[str, str] | None = None,
) -> list[Model]:
    if timeout <= 0:
        raise ValueError(f"timeout must be positive, got {timeout!r}")
    request = Request(
        _models_url(host, port),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
    )

    try:
        with _open_url(request, timeout=timeout) as response:
            body = response.read(_MAX_MODELS_BYTES + 1)
            if len(body) > _MAX_MODELS_BYTES:
                raise RuntimeError(f"{service_name} returned an invalid model response.")
            records = _model_records(body)
    except HTTPError as error:
        if error.code == 401:
            raise RuntimeError(
                f"{service_name} rejected its local API key. Check the Claudex "
                "router settings."
            ) from error
        raise RuntimeError(f"{service_name} returned HTTP {error.code}.") from error
    except (URLError, TimeoutError) as error:
        raise RuntimeError(f"{service_name} is not responding.") from error
    except (ValueError, TypeError, RecursionError, HTTPException, OSError) as error:
        raise RuntimeError(f"{service_name} returned an invalid model response.") from error

    pools = pool_names or set()
    overrides = owner_overrides or {}
    models: list[Model] = []
    for model_id, wire_owner in records:
        is_pool = model_id in pools
        owner = "pool" if is_pool else overrides.get(model_id, wire_owner.strip().lower())
        models.append(Model(model_id, owner, is_pool))

    return _dedup_bare_aliases(sorted(models, key=lambda model: model.id.lower()))


def _dedup_bare_aliases(models: list[Model]) -> list[Model]:
    """Drop bare aliases when a prefixed form with the same tail exists.

    Pool aliases are always kept — they have no upstream prefix.
    """
    tails_with_prefix = {model.id.rsplit("/", 1)[-1] for model in models if "/" in model.id and not model.is_pool}
    return [model for model in models if model.is_pool or "/" in model.id or model.id not in tails_with_prefix]


def fetch_upstream_models(timeout: float = 5.0) -> list[Model]:
    return fetch_models_from(PROXY_HOST, PROXY_PORT, PROXY_API_KEY, "CLIProxyAPI", timeout=timeout)


def fetch_models(
    timeout: float = 5.0,
    pool_names: set[str] | None = None,
    owner_overrides: dict[str, str] | None = None,
) -> list[Model]:
    return fetch_models_from(
        ROUTER_HOST,
        ROUTER_PORT,
        ROUTER_API_KEY,
        "cx router",
        timeout=timeout,
        pool_names=pool_names,
        owner_overrides=owner_overrides,
    )


def category_for(model: Model) -> str:
    if model.is_pool:
        return "Pools"

    owner = model.owner.lower().strip()
    model_id = model.id.lower()
    owner_tokens = set(filter(None, re.split(r"[^a-z0-9]+", owner)))

    for category, names in (("Codex", {"openai", "codex"}), ("Grok", {"xai", "x-ai", "grok"}),
                            ("Kimi", {"kimi", "moonshot"})):
        if owner in names or names & owner_tokens:
            return category

    if model_id.startswith("gpt-"):
        return "Codex"
    if model_id.startswith("grok-"):
        return "Grok"
    if "kimi" in model_id or "moonshot" in model_id:
        return "Kimi"

    return "Custom"


def filter_models(models: list[Model], category: str, query: str) -> list[Model]:
    tokens = [token for token in query.lower().split() if token]
    filtered: list[Model] = []
    for model in models:
        model_category = category_for(model)
        if category != "All" and model_category != category:
            continue
        haystack = f"{model.id} {model.owner} {model_category}".lower()
        if all(token in haystack for token in tokens):
            filtered.append(model)
    return filtered
