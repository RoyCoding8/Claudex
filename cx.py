from __future__ import annotations

import os
import sys
import traceback
import webbrowser

from modules.config import CONFIG_ERRORS
from modules.launcher import launch_claude
from modules.models import Model, category_for, fetch_models, fetch_upstream_models
from modules.pool_tui import run_pool_manager
from modules.pools import load_pools, pool_names, validate_pools_against_models
from modules.proxy import ensure_proxy
from modules.router_starter import ensure_router, read_router_pid, router_is_ready, stop_router
from modules.tui import run_picker


def clear_console() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def pause_on_error(message: str) -> None:
    clear_console()
    print("Claudex could not refresh its configuration or model list\n")
    print(message)
    input("\nPress Enter to retry...")


def _log_traceback(error: Exception) -> None:
    from modules.config import DATA_DIR
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with (DATA_DIR / "router.log").open("a", encoding="utf-8") as handle:
            traceback.print_exception(type(error), error, error.__traceback__, file=handle)
    except OSError:
        pass


def _was_interrupted(exit_code: int) -> bool:
    return exit_code in {130, -2, -1073741510, 3221225786}


def _is_openai_family(model: Model, upstream_models: list[Model],
                      current_pools) -> bool:
    """Judge the family from provider metadata, never from the user-chosen alias."""
    if model.is_pool:
        members = next((pool.members for pool in current_pools if pool.name == model.id), ())
        resolved = [upstream for upstream in upstream_models
                    if upstream.id in {member.model for member in members}]
        return bool(resolved) and all(category_for(upstream) == "Codex" for upstream in resolved)
    return category_for(model) == "Codex"


def _open_management() -> None:
    from modules.config import PROXY_HOST, PROXY_PORT
    url = f"http://{PROXY_HOST}:{PROXY_PORT}/management.html"
    opened = webbrowser.open(url)
    clear_console()
    print("CLIProxyAPI management\n\n" + url)
    if not opened:
        print("\nThe browser did not open automatically; use the URL above.")
    input("\nAfter saving provider changes, press Enter to refresh Claudex...")


def _router_console() -> None:
    clear_console()
    pid, running = read_router_pid(), router_is_ready()
    print("cx router\n")
    if running:
        print(f"  Status: running (PID {pid})")
        print("\n  ⚠ Active Claude Code sessions may lose in-flight responses")
        choice = input("\n  [K] Kill router   [R] Restart router   [Enter] Cancel\n\n  Choice: ").strip().lower()
        if choice in {"k", "kill"}:
            if stop_router():
                print("\n  Router stopped.")
            else:
                print("\n  Router did not stop — check data/router.log.")
            input("  Press Enter to return...")
        elif choice in {"r", "restart"}:
            stopped = stop_router()
            print(f"\n  Router {'stopped' if stopped else 'did not stop'}; restarting...")
    else:
        print("  Status: not currently ready\n\n  Router will be rechecked on refresh.")
        input("  Press Enter to return...")


def _configure_extra_models(model_list: list[Model]) -> None:
    from modules.tui import set_extra_model
    for key, title in (("gpt_fast_model", "[Fast/Haiku]"), ("gpt_medium_model", "[Medium/Sonnet]"), ("gpt_subagent_model", "[Subagent]")):
        selected = run_picker(model_list, picker_title=f"{title} Select model or pool · Del clear · Esc cancel", sub_picker=True)
        if selected.action == "cancel":
            break
        if selected.action == "launch" and selected.model:
            set_extra_model(key, selected.model.id)
        elif selected.action == "clear":
            set_extra_model(key, None)


def main() -> int:
    if CONFIG_ERRORS:
        print("Configuration problems in .env:")
        for problem in CONFIG_ERRORS:
            print(f"  - {problem}")
        return 1
    extra_arguments = sys.argv[1:]
    while True:
        try:
            ensure_proxy()
            upstream_models = fetch_upstream_models()
            if not upstream_models:
                raise RuntimeError("CLIProxyAPI returned no models. Check provider configuration, then retry.")
            ensure_router()
            current_pools = load_pools(upstream_models=upstream_models)
            for warning in validate_pools_against_models(current_pools, upstream_models):
                print(f"Warning: {warning}")
            model_list = fetch_models(
                pool_names=pool_names(current_pools),
                owner_overrides={model.id: model.owner for model in upstream_models},
            )
            if not model_list:
                raise RuntimeError("cx router returned no models. The previous list was unavailable; retry after CLIProxyAPI recovers.")

            result = run_picker(model_list)
            if result.action == "exit":
                return 0
            if result.action == "refresh":
                continue
            if result.action == "pools":
                run_pool_manager(upstream_models)
                continue
            if result.action == "management":
                _open_management()
                continue
            if result.action == "gateway":
                _router_console()
                continue
            if result.action == "model_parameters" and result.model:
                from modules.tui import configure_model_parameters
                clear_console()
                configure_model_parameters(result.model.id)
                continue
            if result.action == "configure":
                _configure_extra_models(model_list)
                continue
            if result.action == "launch" and result.model:
                clear_console()
                exit_code = launch_claude(
                    result.model.id, result.skip_permissions, result.context_tokens,
                    result.auto_compact, extra_arguments, result.gpt_fast_model,
                    result.gpt_medium_model, result.gpt_subagent_model,
                    openai_family=_is_openai_family(result.model, upstream_models, current_pools),
                )
                if exit_code != 0 and not _was_interrupted(exit_code):
                    print(f"\nClaude Code exited with code {exit_code}.")
                    input("Press Enter to return to Claudex...")
        except KeyboardInterrupt:
            return 130
        except Exception as error:
            if not isinstance(error, RuntimeError):
                _log_traceback(error)
                error = RuntimeError(f"Unexpected {type(error).__name__}: {error}")
            pause_on_error(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
