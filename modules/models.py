"""Model discovery and categorisation.

Models come from CLIProxyAPI (``/v1/models``); pool names come from the local
``pools.json`` and are merged in as synthetic entries with ``owner='pool'``.

Two fetch functions exist so callers can choose their upstream:

    * ``fetch_upstream_models`` — hits CLIProxyAPI directly. Used by the pool
      UI, the router (indirectly), and pool validation.
    * ``fetch_models``           — hits the cx router. Used by the model
      picker after the router is up, so users see the exact set the router
      would honour (upstream models + our pool aliases).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import (
    PROXY_API_KEY,
    PROXY_HOST,
    PROXY_PORT,
    ROUTER_API_KEY,
    ROUTER_HOST,
    ROUTER_PORT,
)


@dataclass(frozen=True, slots=True)
class Model:
    id: str
    owner: str
    is_pool: bool = False


CATEGORIES = ("All", "Pools", "Codex", "Grok", "Kimi", "Custom")


def _models_url(host: str, port: int) -> str:
    return f"http://{host}:{port}/v1/models"


def fetch_models_from(
    host: str,
    port: int,
    api_key: str,
    service_name: str,
    timeout: float = 5.0,
    pool_names: set[str] | None = None,
    owner_overrides: dict[str, str] | None = None,
) -> list[Model]:
    request = Request(
        _models_url(host, port),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
    )

    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except HTTPError as error:
        if error.code == 401:
            raise RuntimeError(
                f"{service_name} rejected its local API key. Check the Claudex "
                "router settings."
            ) from error
        raise RuntimeError(f"{service_name} returned HTTP {error.code}.") from error
    except (URLError, TimeoutError) as error:
        raise RuntimeError(f"{service_name} is not responding.") from error
    except (json.JSONDecodeError, TypeError) as error:
        raise RuntimeError(f"{service_name} returned an invalid model response.") from error

    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise RuntimeError(f"{service_name} returned an invalid model response.")

    pools = pool_names or set()
    overrides = owner_overrides or {}
    by_id: dict[str, Model] = {}
    for item in payload["data"]:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id", "")).strip()
        if not model_id:
            continue
        owner = overrides.get(
            model_id, str(item.get("owned_by", "")).strip().lower()
        )
        is_pool = model_id in pools or owner == "pool"
        if is_pool:
            owner = "pool"
        by_id.setdefault(model_id, Model(model_id, owner, is_pool))

    return _dedup_bare_aliases(sorted(by_id.values(), key=lambda model: model.id.lower()))


def _dedup_bare_aliases(models: list[Model]) -> list[Model]:
    """Drop bare aliases when a prefixed form with the same tail exists.

    CLIProxyAPI often exposes the same underlying model twice — once under its
    provider prefix (e.g. ``vercel/openai/gpt-5.4``) and once as a bare alias
    (``gpt-5.4``). The bare form is ambiguous when several providers offer the
    same tail name, and pool members always reference the prefixed form, so
    the picker becomes clearer if we hide the bare aliases whenever a
    prefixed form is available.

    Pool aliases (``is_pool``) are always kept — they have no upstream prefix
    and are the router's own routable names.
    """
    tails_with_prefix = {
        model.id.rsplit("/", 1)[-1]
        for model in models
        if "/" in model.id and not model.is_pool
    }
    return [
        model for model in models
        if model.is_pool
        or "/" in model.id
        or model.id not in tails_with_prefix
    ]


def fetch_upstream_models(timeout: float = 5.0) -> list[Model]:
    return fetch_models_from(
        PROXY_HOST,
        PROXY_PORT,
        PROXY_API_KEY,
        "CLIProxyAPI",
        timeout=timeout,
    )


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
    if model.is_pool or model.owner == "pool":
        return "Pools"

    owner = model.owner.lower().strip()
    model_id = model.id.lower()
    owner_tokens = set(filter(None, re.split(r"[^a-z0-9]+", owner)))

    if owner in {"openai", "codex"} or {"openai", "codex"} & owner_tokens:
        return "Codex"
    if owner in {"xai", "x-ai", "grok"} or {"xai", "grok"} & owner_tokens:
        return "Grok"
    if owner in {"kimi", "moonshot"} or {"kimi", "moonshot"} & owner_tokens:
        return "Kimi"

    # Fallback: aggregator owners (SiliconFlow, OpenRouter, NIM) hide the real
    # origin, so guess from the model id too.
    if model_id.startswith("gpt-"):
        return "Codex"
    if model_id.startswith("grok-"):
        return "Grok"
    if "kimi" in model_id or "moonshot" in model_id:
        return "Kimi"

    return "Custom"


def filter_models(models: list[Model], category: str, query: str) -> list[Model]:
    tokens = [token for token in query.lower().split() if token]
    result: list[Model] = []

    for model in models:
        model_category = category_for(model)
        if category != "All" and model_category != category:
            continue

        haystack = f"{model.id} {model.owner} {model_category}".lower()
        if all(token in haystack for token in tokens):
            result.append(model)

    return result
