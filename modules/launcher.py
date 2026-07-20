from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .config import (
    DEFAULT_COMPACT_WINDOW,
    DEFAULT_GPT_FAST_MODEL,
    DEFAULT_GPT_MEDIUM_MODEL,
    DEFAULT_GPT_SUBAGENT_MODEL,
    ROUTER_API_KEY,
    ROUTER_HOST,
    ROUTER_PORT,
)


def _clean_extra_args(arguments: list[str]) -> list[str]:
    cleaned: list[str] = []
    skip_next = False

    for argument in arguments:
        if skip_next:
            skip_next = False
            continue
        if argument == "--dangerously-skip-permissions":
            continue
        if argument == "--model":
            skip_next = True
            continue
        if argument.startswith("--model="):
            continue
        cleaned.append(argument)

    return cleaned


def launch_claude(
    model_id: str,
    skip_permissions: bool,
    context_tokens: int | None,
    auto_compact: bool | None,
    extra_arguments: list[str],
    gpt_fast_model: str | None = None,
    gpt_medium_model: str | None = None,
    gpt_subagent_model: str | None = None,
) -> int:
    claude = shutil.which("claude")
    if not claude:
        raise RuntimeError(
            "The claude command was not found. Install Claude Code or add it to PATH."
        )

    environment = os.environ.copy()
    if context_tokens:
        environment["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = str(context_tokens)

    if auto_compact is False:
        environment["DISABLE_AUTO_COMPACT"] = "1"
        environment["DISABLE_COMPACT"] = "1"
    else:
        # Force standard autocompact behavior for custom models by explicitly setting the window
        window = int(context_tokens * 0.85) if context_tokens else DEFAULT_COMPACT_WINDOW
        environment["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(window)
    environment["ANTHROPIC_BASE_URL"] = f"http://{ROUTER_HOST}:{ROUTER_PORT}"
    environment["ANTHROPIC_AUTH_TOKEN"] = ROUTER_API_KEY
    environment.pop("ANTHROPIC_API_KEY", None)
    environment.pop("CLAUDE_CODE_EFFORT_LEVEL", None)
    environment["CLAUDE_CODE_DISABLE_AGENT_VIEW"] = "1"
    environment["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"] = "1"
    environment["ANTHROPIC_MODEL"] = model_id
    
    if "gpt" in model_id.lower() or "openai" in model_id.lower():
        fast = gpt_fast_model or DEFAULT_GPT_FAST_MODEL or model_id
        medium = gpt_medium_model or DEFAULT_GPT_MEDIUM_MODEL or model_id
        subagent = gpt_subagent_model or DEFAULT_GPT_SUBAGENT_MODEL or model_id
        environment["ANTHROPIC_SMALL_FAST_MODEL"] = fast
        environment["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = fast
        environment["ANTHROPIC_DEFAULT_SONNET_MODEL"] = medium
        environment["ANTHROPIC_DEFAULT_OPUS_MODEL"] = model_id
        environment["CLAUDE_CODE_SUBAGENT_MODEL"] = subagent
    else:
        fast = gpt_fast_model or model_id
        medium = gpt_medium_model or model_id
        subagent = gpt_subagent_model or model_id
        environment["ANTHROPIC_SMALL_FAST_MODEL"] = fast
        environment["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = fast
        environment["ANTHROPIC_DEFAULT_SONNET_MODEL"] = medium
        environment["ANTHROPIC_DEFAULT_OPUS_MODEL"] = model_id
        environment["CLAUDE_CODE_SUBAGENT_MODEL"] = subagent

    command = [claude, "--model", model_id]
    if skip_permissions:
        command.append("--dangerously-skip-permissions")
    command.extend(_clean_extra_args(extra_arguments))

    if Path(claude).suffix.lower() in {".cmd", ".bat"}:
        command_line = subprocess.list2cmdline(command)
        return subprocess.call(
            [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", command_line],
            env=environment,
        )

    return subprocess.call(command, env=environment)
