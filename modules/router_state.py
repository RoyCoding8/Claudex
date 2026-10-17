from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import process

_LOG = logging.getLogger("cx.router.state")

_STATE_VERSION = 1
_MAX_STATE_BYTES = 1024 * 1024
_MAX_MODELS = 4096
_RATE_WINDOW = 60.0
_MAX_COOLDOWN = 1800.0

class StateUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _ModelState:
    hits: tuple[float, ...] = ()
    cooldown_until: float | None = None


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise ValueError(f"invalid number {value}")


def _finite_number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("expected a number")
    try:
        number = float(value)
    except OverflowError as error:
        raise ValueError("expected a finite number") from error
    if not math.isfinite(number):
        raise ValueError("expected a finite number")
    return number


def _publish_state(path: Path, document: dict[str, Any]) -> None:
    encoded = json.dumps(document, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > _MAX_STATE_BYTES:
        raise ValueError(f"router state exceeds {_MAX_STATE_BYTES} bytes")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
            temporary = handle.name
            handle.write(encoded)
            handle.flush()
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


class RouterState:
    def __init__(
        self,
        path: Path | None = None,
        *,
        wall_time: Callable[[], float] = time.time,
        monotonic_time: Callable[[], float] = time.monotonic,
    ) -> None:
        self._path = path
        self._wall_time = wall_time
        self._monotonic_time = monotonic_time
        self._lock = threading.RLock()
        try:
            self._file_lock = process.lock_file(path.with_name(f".{path.name}.lock")) if path is not None else None
            if path is not None and self._file_lock is None:
                raise StateUnavailableError(f"router state is already locked: {path}")
        except OSError as error:
            raise StateUnavailableError(f"could not lock router state at {path}: {error}") from error
        try:
            self._models = self._load()
        except (OSError, UnicodeError, ValueError, TypeError, RecursionError) as error:
            if self._file_lock is not None:
                self._file_lock.close()
            raise StateUnavailableError(f"could not load router state from {path}: {error}") from error

    def _load(self) -> dict[str, _ModelState]:
        if self._path is None:
            return {}
        try:
            with self._path.open("rb") as handle:
                encoded = handle.read(_MAX_STATE_BYTES + 1)
        except FileNotFoundError:
            return {}
        if len(encoded) > _MAX_STATE_BYTES:
            raise ValueError(f"router state exceeds {_MAX_STATE_BYTES} bytes")
        document = json.loads(encoded.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys,
                              parse_constant=_reject_constant)
        if not isinstance(document, dict) or type(document.get("version")) is not int:
            raise ValueError("invalid router state document")
        if document["version"] != _STATE_VERSION or not isinstance(document.get("models"), dict):
            raise ValueError("unsupported router state document")
        if len(document["models"]) > _MAX_MODELS:
            raise ValueError("router state contains too many models")

        wall_now, monotonic_now = self._wall_time(), self._monotonic_time()
        models: dict[str, _ModelState] = {}
        for model, raw in document["models"].items():
            if not isinstance(model, str) or not model or not isinstance(raw, dict):
                raise ValueError("invalid router state model entry")
            raw_hits = raw.get("hits", [])
            if not isinstance(raw_hits, list):
                raise ValueError("invalid router state hit list")
            hits: list[float] = []
            for raw_hit in raw_hits:
                hit = _finite_number(raw_hit)
                age = max(0.0, wall_now - hit)
                if age < _RATE_WINDOW:
                    hits.append(monotonic_now - age)
            cooldown: float | None = None
            raw_cooldown = raw.get("cooldown_until")
            if raw_cooldown is not None:
                expiry = _finite_number(raw_cooldown)
                remaining = expiry - wall_now
                if remaining > 0:
                    cooldown = monotonic_now + min(remaining, _MAX_COOLDOWN)
            if hits or cooldown is not None:
                models[model] = _ModelState(tuple(sorted(hits)), cooldown)
        return models

    def _document(self, models: dict[str, _ModelState]) -> dict[str, Any]:
        wall_now, monotonic_now = self._wall_time(), self._monotonic_time()
        serialized: dict[str, Any] = {}
        for model, state in models.items():
            hits = [wall_now + hit - monotonic_now for hit in state.hits
                    if monotonic_now - hit < _RATE_WINDOW]
            cooldown = state.cooldown_until
            cooldown_wall = (
                wall_now + cooldown - monotonic_now
                if cooldown is not None and cooldown > monotonic_now else None
            )
            if hits or cooldown_wall is not None:
                serialized[model] = {"hits": hits, "cooldown_until": cooldown_wall}
        if len(serialized) > _MAX_MODELS:
            raise ValueError("router state contains too many models")
        return {"version": _STATE_VERSION, "models": serialized}

    def _save(self) -> None:
        if self._path is not None:
            try:
                _publish_state(self._path, self._document(self._models))
            except (OSError, ValueError, TypeError) as error:
                _LOG.error("router state snapshot failed; live state retained: %s", error)

    def reserve(self, model: str, limit: int | None) -> bool:
        if limit is None:
            return True
        if limit <= 0:
            return False
        with self._lock:
            now = self._monotonic_time()
            current = self._models.get(model, _ModelState())
            hits = tuple(hit for hit in current.hits if hit > now - _RATE_WINDOW)
            if len(hits) >= limit:
                return False
            self._models[model] = _ModelState((*hits, now), current.cooldown_until)
            return True

    def is_ready(self, model: str) -> bool:
        with self._lock:
            state = self._models.get(model)
            if state is None or state.cooldown_until is None:
                return True
            if state.cooldown_until > self._monotonic_time():
                return False
            self._models[model] = _ModelState(state.hits)
            if not state.hits:
                self._models.pop(model)
            return True

    def cooldown(self, model: str, seconds: float) -> None:
        duration = max(1.0, min(seconds, _MAX_COOLDOWN))
        with self._lock:
            now = self._monotonic_time()
            current = self._models.get(model, _ModelState())
            self._models[model] = _ModelState(
                tuple(hit for hit in current.hits if hit > now - _RATE_WINDOW), now + duration)
            self._save()

    def clear(self, model: str) -> None:
        with self._lock:
            current = self._models.get(model)
            if current is None or current.cooldown_until is None:
                return
            if current.hits:
                self._models[model] = _ModelState(current.hits)
            else:
                self._models.pop(model)
            self._save()

    def close(self) -> None:
        with self._lock:
            handle = self._file_lock
            self._file_lock = None
            if handle is not None:
                try:
                    self._save()
                finally:
                    handle.close()
