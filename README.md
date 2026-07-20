# Claudex

Claudex is a small TUI launcher for [Claude Code](https://docs.claude.com/en/docs/claude-code) that
routes Claude Code through a local two-layer proxy so you can point one Claude Code install at any
provider you have credentials for, and pool several providers together behind a single model name.

```text
Claude Code → cx router :4000 → CLIProxyAPI :8317 → providers
```

- **CLIProxyAPI** owns provider accounts, API keys, per-provider prefixes, and model discovery.
- **cx router** (this repo) sits in front of CLIProxyAPI and adds cross-provider *pools*, weighted
  routing, retries, and cooldowns. It speaks Claude Code's Anthropic `/v1/messages` protocol and
  passes everything else through unchanged.

The router is a single-file, stdlib-only Python service (~740 lines, one runtime dependency:
`prompt-toolkit` for the TUI). It boots in ~250ms and requires no build step.

## Quick start

Install CLIProxyAPI (see its own docs), put `cli-proxy-api.exe` and `config.yaml` beside this
project's launcher, or point Claudex at their locations with `CX_CLIPROXY_EXE` and
`CX_CLIPROXY_CONFIG`. Then, from this repo:

```bat
cx.bat
```

The first run uses [`uv`](https://github.com/astral-sh/uv) to install `prompt-toolkit` into a local
`.venv/`. Subsequent runs are instant. `cx.bat`:

1. Starts CLIProxyAPI if it isn't already running.
2. Starts the cx router if it isn't already running.
3. Opens the model picker — merges CLIProxyAPI's `/v1/models` with your local pool aliases.
4. Launches Claude Code pointed at the router.

Claude Code receives `ANTHROPIC_BASE_URL=http://127.0.0.1:4000` and gateway model discovery
enabled, so it uses whichever model or pool you selected.

## Creating a pool

Manage providers, API keys, and prefixes inside CLIProxyAPI. Press **F8** in Claudex to open
CLIProxyAPI's `management.html`.

Press **F7** in the model picker to create cross-provider pools:

1. Name the pool the way you want Claude Code to see it, e.g. `Opus-level`.
2. Add ≥ 2 provider-specific model IDs (`nvidia_nim/z-ai/glm-5.2`, `vercel/zai/glm-5.2`, …).
3. Set the real RPM/TPM limit per member. If unsure, leave blank (auto).
4. Optionally set a priority per member (lower number = tried first).
5. Save and return to the picker.

Pool definitions live in `data/pools.json`. The router reloads them automatically on every request
via `mtime`, so edits take effect without restarting anything.

Selecting a pool in the picker tells Claude Code to send its `model` field as the pool name (e.g.
`Opus-level`). The router rewrites that name to a real backend model per request.

## Router semantics

- **Selection**. Strict priority tiers: the router only considers members at the lowest priority
  number available. Within a tier, it picks weighted-random by `rpm`. A member missing `priority`
  falls to the *default* tier (`0`); missing `rpm` defaults to `1`.
- **Retry**. On a member-scoped failure — `401`/`403`, API-key/auth/rate-limit-shaped `400` or
  first SSE error event, `429` (with `Retry-After` honored), any `5xx`, or a network error — the
  router marks that real backend model as cooling-down and tries the next member once. The budget is
  `min(pool_size, 8)` attempts.
- **Cooldown**. `429` cools by `Retry-After` if provided else 60s; `5xx` cools 30s; a network
  error cools 10s; provider authentication failures cool 300s. Cooldown is keyed by the real
  backend model, so if the same backend appears in multiple pools its cooldown is shared.
- **Streaming**. SSE is forwarded incrementally via `HTTPResponse.read1()` — no buffering. Once
  any bytes have been forwarded to the client, retries are not attempted (they can't be, safely).
- **Passthrough**. If the incoming `model` field is not one of your enabled pool names, the
  request is forwarded unchanged. Pooled `count_tokens` requests use the same failover policy.
- **Errors**. Ordinary request-validation `4xx` responses are forwarded verbatim. If every pool
  member fails, the router returns a sanitized `503` summary without upstream bodies or keys.

## Endpoints

The router speaks Anthropic on `/v1/messages`, passes through `/v1/chat/completions` and
`/v1/completions` for OpenAI-native clients, exposes `/v1/models` (CLIProxyAPI's list + your
pool aliases), and answers `/health`, `/-/ready`, `/-/health`, `/` for probes.

## Ports and keys

Defaults:

| Component     | Address                | Local key      |
|---------------|------------------------|----------------|
| CLIProxyAPI   | `127.0.0.1:8317`       | `sk-dummy`     |
| cx router     | `127.0.0.1:4000`       | `sk-cx-local`  |

Both keys are for local traffic between Claude Code, the router, and CLIProxyAPI on your machine.
Real provider credentials live inside CLIProxyAPI's `config.yaml`, never in this repo.

## Environment overrides

Copy `.env.example` to `.env` and uncomment the
lines you want to change — the launcher and the router both read it at
startup. A value already present in the real environment (shell export /
launcher) always wins over the file, and unset lines fall back to the built-in
defaults. Real provider credentials live in CLIProxyAPI's `config.yaml`, never
in `.env`; this file only configures local traffic. `.env` is git-ignored.

Router:
- `CX_ROUTER_HOST`, `CX_ROUTER_PORT`, `CX_ROUTER_API_KEY`
- `CX_ROUTER_COOLDOWN_429`, `CX_ROUTER_COOLDOWN_5XX`, `CX_ROUTER_COOLDOWN_NETWORK`,
  `CX_ROUTER_COOLDOWN_AUTH` (seconds, clamped to 1–1800)
- Legacy aliases still accepted: `CX_LITELLM_HOST`, `CX_LITELLM_PORT`, `CX_LITELLM_API_KEY`

CLIProxyAPI:
- `CX_CLIPROXY_EXE`, `CX_CLIPROXY_CONFIG`
- `CX_CLIPROXY_HOST`, `CX_CLIPROXY_PORT`, `CX_CLIPROXY_API_KEY`

## Files

```
cx.py                       # entry point: orchestrates proxy + router + picker
cx.bat                      # Windows launcher (uv sync + cx.py)
.env.example                # env var template (tracked); copy to .env (ignored)
modules/
  proxy.py                  # CLIProxyAPI lifecycle
  router.py                 # the pool router (Anthropic /v1/messages + passthrough)
  router_starter.py         # router lifecycle (health check + subprocess spawn)
  pools.py                  # pools.json schema + load/save + validation
  models.py                 # /v1/models fetch, dedup, categorization
  launcher.py               # spawn Claude Code with ANTHROPIC_BASE_URL set
  tui.py                    # model picker
  pool_tui.py               # F7 pool editor
  config.py                 # env-driven config
data/
  pools.example.json        # publishable starter template (tracked)
  settings.example.json     # publishable starter template (tracked)
  pools.json                # your local pool definitions (ignored)
  settings.json             # your local picker state (ignored)
  router.log                # router runtime log (ignored)
  router.pid                # router pid (ignored)
tests/
  test_router.py            # pool pick + body/header rewrite + parse
  test_pools.py             # pools.json schema + validation
  test_models.py            # /v1/models fetch + dedup
  test_launcher.py          # Claude Code env setup
```

## Running the tests

```bat
uv run --project . python -m unittest discover tests
```

## Multi-session support

The router is a long-lived subprocess (PID in `data/router.pid`, log in `data/router.log`). A
second `cx.bat` in another terminal detects the running router and reuses it. Only one CLIProxyAPI
process and one router process are ever needed regardless of how many Claude Code sessions you
have open.

## Logs

- `data/cli-proxy-api.log` — CLIProxyAPI
- `data/router.log` — cx router (per-request selection, cooldowns, retries)

Logs are local and ignored, but they can contain filesystem paths, model IDs, and provider error
summaries. Review or redact them before sharing.

## License

Apache 2.0
