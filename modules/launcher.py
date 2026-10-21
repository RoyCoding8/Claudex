from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .config import (
    ACTIVE_CLIENT,
    CLIENTS,
    DEFAULT_COMPACT_WINDOW,
    ROUTER_API_KEY,
    ROUTER_HOST,
    ROUTER_PORT,
)
from .urls import http_url


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _clean_extra_args(arguments: list[str], client: str | None = None) -> list[str]:
    client = client or ACTIVE_CLIENT
    value_options = {"--model", "-m"}
    if client == "codex":
        flag_options = {"--dangerously-bypass-approvals-and-sandbox", "--yolo", "--full-auto"}
        value_options |= {"--ask-for-approval", "-a", "--sandbox", "-s"}
    else:
        flag_options = {"--dangerously-skip-permissions"}
        value_options.add("--permission-mode")

    cleaned: list[str] = []
    skip_next = False
    after_separator = False
    for argument in arguments:
        if after_separator:
            cleaned.append(argument)
            continue
        if argument == "--":
            after_separator = True
            cleaned.append(argument)
            continue
        if skip_next:
            skip_next = False
            continue
        if argument in flag_options:
            continue
        if argument in value_options:
            skip_next = True
            continue
        if argument.startswith("--model=") or argument.startswith("-m="):
            continue
        if argument.startswith((
            "--permission-mode=", "--ask-for-approval=", "--sandbox=", "-a=", "-s=",
        )):
            continue
        cleaned.append(argument)
    return cleaned


def _no_proxy_value(environment: dict[str, str], host: str) -> str:
    entries = [
        entry.strip()
        for variable in ("NO_PROXY", "no_proxy")
        for entry in environment.get(variable, "").split(",")
        if entry.strip()
    ]
    entries.append(host)
    return ",".join(dict.fromkeys(entries))


def _child_environment() -> dict[str, str]:
    environment = os.environ.copy()
    no_proxy = _no_proxy_value(environment, ROUTER_HOST)
    for variable in tuple(environment):
        if variable.upper().startswith("CX_"):
            environment.pop(variable)
    environment.update({"NO_PROXY": no_proxy, "no_proxy": no_proxy})
    return environment


def _call_cli(executable: str, arguments: list[str], environment: dict[str, str]) -> int:
    command = [executable, *arguments]
    try:
        if Path(executable).suffix.lower() == ".ps1":
            return subprocess.call(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", executable, *arguments],
                env=environment,
            )
        return subprocess.call(command, env=environment)
    except OSError as error:
        raise RuntimeError(f"Failed to launch {executable}: {error}") from error


def _launch_claude(
    model_id: str,
    skip_permissions: bool,
    context_tokens: int | None,
    auto_compact: bool | None,
    extra_arguments: list[str],
    role_models: dict[str, str],
    openai_family: bool,
) -> int:
    executable = shutil.which("claude")
    if not executable:
        raise RuntimeError("The claude command was not found. Install Claude Code or add it to PATH.")

    environment = _child_environment()
    for variable in (
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS",
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
        "DISABLE_AUTO_COMPACT",
        "DISABLE_COMPACT",
    ):
        environment.pop(variable, None)
    if context_tokens:
        environment["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = str(context_tokens)

    if auto_compact is False:
        environment["DISABLE_AUTO_COMPACT"] = "1"
        environment["DISABLE_COMPACT"] = "1"
    elif auto_compact is True:
        window = int(context_tokens * 0.85) if context_tokens else DEFAULT_COMPACT_WINDOW
        environment["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(window)

    _, roles = CLIENTS["claude"]
    for key, _label, target, openai_default, _description in roles:
        chosen = role_models.get(key)
        fallback = openai_default if openai_family else ""
        value = chosen or fallback or model_id
        for variable in target[1:]:
            environment[variable] = value

    environment.update({
        "ANTHROPIC_BASE_URL": http_url(ROUTER_HOST, ROUTER_PORT),
        "ANTHROPIC_AUTH_TOKEN": ROUTER_API_KEY,
        "ANTHROPIC_MODEL": model_id,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": model_id,
        "CLAUDE_CODE_DISABLE_AGENT_VIEW": "1",
        "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1",
    })
    environment.pop("ANTHROPIC_API_KEY", None)
    environment.pop("CLAUDE_CODE_EFFORT_LEVEL", None)
    for variable in (
        "CLAUDE_CODE_USE_ANTHROPIC_AWS",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_USE_MANTLE",
        "CLAUDE_CODE_USE_VERTEX",
    ):
        environment.pop(variable, None)

    arguments = ["--model", model_id]
    if skip_permissions:
        arguments.append("--dangerously-skip-permissions")
    arguments.extend(_clean_extra_args(extra_arguments, "claude"))
    return _call_cli(executable, arguments, environment)


def _launch_codex(
    model_id: str,
    skip_permissions: bool,
    context_tokens: int | None,
    auto_compact: bool | None,
    extra_arguments: list[str],
    role_models: dict[str, str],
) -> int:
    executable = shutil.which("codex")
    if not executable:
        raise RuntimeError("The codex command was not found. Install Codex or add it to PATH.")

    environment = _child_environment()
    environment["CX_ROUTER_API_KEY"] = ROUTER_API_KEY
    config = {
        "model_provider": '"cx"',
        "model_providers.cx.name": '"cx"',
        "model_providers.cx.base_url": _toml_string(http_url(ROUTER_HOST, ROUTER_PORT, "/v1")),
        "model_providers.cx.wire_api": '"responses"',
        "model_providers.cx.env_key": '"CX_ROUTER_API_KEY"',
        "model_providers.cx.requires_openai_auth": "false",
        "model_providers.cx.supports_websockets": "false",
        "model_providers.cx.capabilities.remote_compaction": '"unsupported"',
    }
    if context_tokens:
        config["model_context_window"] = str(context_tokens)
    if auto_compact is True:
        threshold = int(context_tokens * 0.85) if context_tokens else DEFAULT_COMPACT_WINDOW
        config["model_auto_compact_token_limit"] = str(threshold)

    roles = CLIENTS["codex"][1]
    role_files = tempfile.TemporaryDirectory(prefix="cx-codex-roles-")
    try:
        role_directory = Path(role_files.name)
        for key, _label, target, _openai_default, description in roles:
            value = role_models.get(key) or model_id
            if target[0] == "config":
                config[target[1]] = _toml_string(value)
            elif target[0] == "agent":
                path = role_directory / f"{target[1]}.toml"
                path.write_text(f"model = {_toml_string(value)}\n", encoding="utf-8")
                config[f"agents.{target[1]}.description"] = _toml_string(description)
                config[f"agents.{target[1]}.config_file"] = _toml_string(str(path))

        arguments = ["--model", model_id]
        if skip_permissions:
            arguments.append("--dangerously-bypass-approvals-and-sandbox")
        for key, value in config.items():
            arguments.extend(("-c", f"{key}={value}"))
        arguments.extend(_clean_extra_args(extra_arguments, "codex"))
        return _call_cli(executable, arguments, environment)
    finally:
        role_files.cleanup()


def launch_client(
    model_id: str,
    skip_permissions: bool,
    context_tokens: int | None,
    auto_compact: bool | None,
    extra_arguments: list[str],
    role_models: dict[str, str] | None = None,
    openai_family: bool = False,
) -> int:
    selected_roles = role_models or {}
    if ACTIVE_CLIENT == "codex":
        return _launch_codex(
            model_id, skip_permissions, context_tokens, auto_compact, extra_arguments, selected_roles
        )
    return _launch_claude(
        model_id, skip_permissions, context_tokens, auto_compact, extra_arguments, selected_roles, openai_family
    )
