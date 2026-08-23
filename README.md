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

The router is a single-file, stdlib-only Python service (~870 lines). The launcher adds two small
runtime dependencies: `prompt-toolkit` for the TUI and `python-dotenv` for optional `.env` loading.
It boots in ~250ms and requires no build step.

## Quick start

Install CLIProxyAPI (see its own docs), put `cli-proxy-api.exe` and `config.yaml` beside this
project's launcher, or point Claudex at their locations with `CX_CLIPROXY_EXE` and
`CX_CLIPROXY_CONFIG`. Then, from this repo:

```bat
cx.bat
```

The first run uses [`uv`](https://github.com/astral-sh/uv) to install `prompt-toolkit` and
`python-dotenv` into a local `.venv/`. Subsequent runs are instant. `cx.bat`:

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
5. Pick a routing strategy: `fill-first` (default) or `round-robin`.
6. Save and return to the picker.

Pool definitions live in `data/pools.json`. The router reloads them automatically on every request
via `mtime`, so edits take effect without restarting anything.

Selecting a pool in the picker tells Claude Code to send its `model` field as the pool name (e.g.
`Opus-level`). The router rewrites that name to a real backend model per request.

## Model parameters

Highlight a model or pool and press **F10** to edit its per-model launch settings:

- **Context size** writes `context_tokens` under that model in `data/settings.json` and sets
  `CLAUDE_CODE_MAX_CONTEXT_TOKENS` when launched. Commas and underscores are accepted.
- **Auto-compact** can be `on`, `off`, or `default`. `off` disables compacting; `on` uses 85% of
  the configured context size as the auto-compact window; `default` removes the override.

Press Enter to keep the current value or type `clear` to restore its default. The editor preserves
other models and unknown per-model keys. In the picker, **Esc** clears a non-empty search first and
exits (or cancels a sub-picker) when the search is already empty.

## Router semantics

- **Selection**. Strict priority tiers are preserved within each availability class: ready/unpaced members are preferred first, then paced-out members, then cooling members. Within the selected class, the router only considers members at the lowest priority number and picks weighted-random by `rpm`. A member missing `priority` falls to the *default* tier (`0`); missing `rpm` defaults to `1`. A pool with `"strategy": "round-robin"` instead cycles through its members evenly in config order — priority tiers and `rpm` weights are ignored, while the availability preferences above still apply. The rotation cursor tracks member *identity* and advances once per request, parking just past whichever member was actually dispatched: a member that served a retry is not handed the next request as well, and a member skipped while cooling reclaims its own slot once it recovers.
- **Pacing**. A member with an explicit `rpm` (or a `limit` override) is *proactively rate-limited*: once that many requests have been sent in the trailing 60 s window, the member is deprioritized so another candidate can serve the request instead of burning a `429`. The sliding window ages out on its own, so the member becomes preferred again when its oldest request falls out of it.
- **Retry**. Pool failover is protocol-driven, not provider-message-driven. Any non-`2xx` response, network error, or SSE `error` **frame envelope** seen before the commit point cools that backend and tries the next member. The router reads only envelopes (`event:` and top-level JSON `type`), never assistant text, so a model may safely discuss `event: error`. It tries every pool member once, in priority order, unless the request-wide deadline (180 s, `CX_ROUTER_POOL_TIMEOUT`) is hit; each attempt's upstream wait is clamped to what remains of that budget.
- **Empty responses**. A `200` carrying no usable content is a failure, not an answer: zero bytes, no complete SSE frame, an unparseable body, `{}`, `{"content": []}`, or a stream that reaches `message_stop` without a single content block. Any of these cools the member (20 s, `CX_ROUTER_COOLDOWN_EMPTY`) and fails over, so a flaky provider returning empties can no longer stall Claude Code. `count_tokens` is exempt from the content check — its success body legitimately has none.
- **Cooldown**. `429` honors `Retry-After`; absent that, a member with a per-minute cap uses the short paced-429 cooldown (10 s, `CX_ROUTER_COOLDOWN_PACED_429`) and all other members use 60 s. A per-member `cooldown` overrides either default. A cooldown is an ordering preference rather than a pool-wide outage: if all alternatives are cooling, each is retried before the router returns `503`.
- **Streaming**. The router holds an SSE response until it proves it carries content — the first `content_block_start`/`content_block_delta` — then replays the buffered head and streams the rest incrementally via `HTTPResponse.read1()`. Committing therefore costs only the provider's real time-to-first-token, and the client still receives the stream from its very first frame. Once bytes have been forwarded, retrying is unsafe; a later upstream interruption is emitted as a final SSE `error` event, logged, and cools the member so the next request avoids it. Non-streaming responses are buffered only long enough to restore `Content-Length`, allowing clients to distinguish complete and truncated responses.
- **Passthrough**. If the incoming `model` field is not one of your enabled pool names, the request is forwarded unchanged. Pooled `count_tokens` requests use the same failover policy.
- **Errors**. Non-pooled requests preserve upstream responses unchanged. A pool returns a sanitized `503` only after every member was attempted or the request deadline expired. The response includes a correlation `request_id`; upstream error bodies are logged locally (bounded) and are never returned to Claude Code.

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
  `CX_ROUTER_COOLDOWN_AUTH`, `CX_ROUTER_COOLDOWN_PACED_429`, `CX_ROUTER_COOLDOWN_EMPTY`
  (seconds, clamped to 1–1800)
- `CX_ROUTER_POOL_TIMEOUT` (seconds, default 180) — whole-request budget shared by every
  failover attempt
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
  test_router.py            # pool selection, forwarding, failover, streaming
  test_tui.py               # picker exit safety + model parameter editing
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
