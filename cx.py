from __future__ import annotations

import os
import sys
import webbrowser

from modules.launcher import launch_claude
from modules.models import fetch_models, fetch_upstream_models
from modules.pools import load_pools, pool_names
from modules.pool_tui import run_pool_manager
from modules.proxy import ensure_proxy
from modules.router_starter import ensure_router, read_router_pid, router_is_ready, stop_router
from modules.tui import run_picker


def clear_console() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def pause_on_error(message: str) -> None:
    clear_console()
    print("Claudex error\n")
    print(message)
    input("\nPress Enter to exit...")


def main() -> int:
    extra_arguments = sys.argv[1:]

    while True:
        try:
            ensure_proxy()
            upstream_models = fetch_upstream_models()
            if not upstream_models:
                raise RuntimeError("CLIProxyAPI returned no models.")

            ensure_router()

            current_pools = load_pools(upstream_models=upstream_models)
            owner_overrides = {model.id: model.owner for model in upstream_models}
            model_list = fetch_models(
                pool_names=pool_names(current_pools),
                owner_overrides=owner_overrides,
            )
            if not model_list:
                raise RuntimeError("cx router returned no models.")

            result = run_picker(model_list)

            if result.action == "exit":
                return 0
            if result.action == "refresh":
                continue
            if result.action == "pools":
                # Pool edits are picked up by the router via mtime hot-reload; no restart needed.
                run_pool_manager(upstream_models)
                continue
            if result.action == "management":
                from modules.config import PROXY_HOST, PROXY_PORT

                url = f"http://{PROXY_HOST}:{PROXY_PORT}/management.html"
                opened = webbrowser.open(url)
                clear_console()
                print("CLIProxyAPI management\n")
                print(url)
                if not opened:
                    print("\nThe browser did not open automatically; use the URL above.")
                input("\nAfter saving provider changes, press Enter to refresh Claudex...")
                continue
            if result.action == "gateway":
                clear_console()
                pid = read_router_pid()
                running = router_is_ready()
                print("cx router\n")
                if running:
                    print(f"  Status: running (PID {pid})")
                    print("\n  \u26a0 Active Claude Code sessions may lose in-flight responses")
                    print("\n  [K] Kill router   [R] Restart router   [Esc/Enter] Cancel")
                    choice = input("\n  Choice: ").strip().lower()
                    if choice in {"k", "kill"}:
                        stop_router()
                        print("\n  Router stopped.")
                        input("  Press Enter to return...")
                    elif choice in {"r", "restart"}:
                        stop_router()
                        print("\n  Router stopped. Restarting...")
                        # Loop continues → ensure_router will restart it.
                else:
                    print("  Status: stopped")
                    print("\n  Router will start automatically on next refresh.")
                    input("  Press Enter to return...")
                continue
            if result.action == "configure":
                from modules.tui import set_extra_model

                fast_res = run_picker(
                    model_list,
                    picker_title="[Fast/Haiku] Select model or pool · Del clear · Esc cancel",
                    sub_picker=True,
                )
                if fast_res.action == "quit":
                    return 0
                if fast_res.action == "cancel":
                    continue
                if fast_res.action == "launch" and fast_res.model:
                    set_extra_model("gpt_fast_model", fast_res.model.id)
                elif fast_res.action == "clear":
                    set_extra_model("gpt_fast_model", None)

                med_res = run_picker(
                    model_list,
                    picker_title="[Medium/Sonnet] Select model or pool · Del clear · Esc cancel",
                    sub_picker=True,
                )
                if med_res.action == "quit":
                    return 0
                if med_res.action == "cancel":
                    continue
                if med_res.action == "launch" and med_res.model:
                    set_extra_model("gpt_medium_model", med_res.model.id)
                elif med_res.action == "clear":
                    set_extra_model("gpt_medium_model", None)

                sub_res = run_picker(
                    model_list,
                    picker_title="[Subagent] Select model or pool · Del clear · Esc cancel",
                    sub_picker=True,
                )
                if sub_res.action == "quit":
                    return 0
                if sub_res.action == "cancel":
                    continue
                if sub_res.action == "launch" and sub_res.model:
                    set_extra_model("gpt_subagent_model", sub_res.model.id)
                elif sub_res.action == "clear":
                    set_extra_model("gpt_subagent_model", None)

                continue
            if result.action == "launch" and result.model:
                clear_console()
                exit_code = launch_claude(
                    result.model.id,
                    result.skip_permissions,
                    result.context_tokens,
                    result.auto_compact,
                    extra_arguments,
                    result.gpt_fast_model,
                    result.gpt_medium_model,
                    result.gpt_subagent_model,
                )
                if exit_code != 0:
                    print(f"\nClaude Code exited with code {exit_code}.")
                    input("Press Enter to return to Claudex...")

        except KeyboardInterrupt:
            return 130
        except RuntimeError as error:
            pause_on_error(str(error))
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
