<p align="center">
  <img src="assets/logo.svg" alt="Claudex" width="96" height="96">
</p>

<h1 align="center">Claudex</h1>

<p align="center">
  A TUI launcher that routes Claude Code through a local proxy and pools providers behind one model name.
</p>

<p align="center">
  <a href="https://github.com/RoyCoding8/Claudex/actions/workflows/tests.yml"><img alt="tests" src="https://github.com/RoyCoding8/Claudex/actions/workflows/tests.yml/badge.svg"></a>
  <a href="https://github.com/RoyCoding8/Claudex/blob/main/LICENSE"><img alt="license" src="https://img.shields.io/badge/license-Apache%202.0-blue.svg"></a>
  <img alt="dependencies" src="https://img.shields.io/badge/dependencies-2-blue.svg">
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> ·
  <a href="#creating-a-pool">Pools</a> ·
  <a href="#router-semantics">Router</a> ·
  <a href="#environment-overrides">Config</a> ·
  <a href="#running-the-tests">Tests</a>
</p>

---

<p align="center">
  <img src="assets/architecture.svg" alt="Claude Code and Codex route through the cx router on port 4000, then through CLIProxyAPI on port 8317, then to your providers" width="760">
</p>

**CLIProxyAPI** owns provider accounts, API keys, per-provider prefixes, and model
discovery. **cx router** (this repo) sits in front of it and adds cross-provider
*pools*, weighted routing, retries, and cooldowns. It pools the three wire formats
CLIProxyAPI serves: Anthropic `/v1/messages`, OpenAI `/v1/responses` (what Codex
speaks), and `/v1/chat/completions`. It forwards `/v1/completions` unchanged.
Unknown routes return `404`.

The router is stdlib-only and lives in `modules/router.py`. The launcher adds two
small runtime dependencies: `prompt-toolkit` for the TUI and `python-dotenv` for
optional `.env` loading. There is no build step.

| | |
|---|---|
| **Router** | stdlib only, `modules/router.py` |
| **Runtime deps** | `prompt-toolkit`, `python-dotenv` |
| **Build step** | none |
| **Tests** | 533, hermetic, on Windows, macOS and Linux |

## Quick start

Install CLIProxyAPI (see its own docs) somewhere **outside this repository**, then point Claudex at
it. `config.yaml` holds your provider API keys, so keep it out of a working tree that is under
version control:

```sh
CX_CLIPROXY_EXE=/path/to/cli-proxy-api
CX_CLIPROXY_CONFIG=/path/to/config.yaml
```

Put those in `.env` (gitignored) or export them. Dropping the binary beside this launcher still
works and is gitignored, but a checkout is the wrong home for a 68 MB executable and a file of
credentials. Then, from this repo:

```bat
cx.bat
```

on Windows, or on Linux and macOS:

```sh
chmod +x cx.sh   # first time only
./cx.sh
```

Both wrappers take the same arguments and pass anything they don't consume through to Claude Code.
`cx.sh` resolves symlinks, so you can link it onto your `PATH`
(`ln -s "$PWD/cx.sh" ~/.local/bin/cx`) and run `cx` from any directory.

The first run uses [`uv`](https://github.com/astral-sh/uv) to install `prompt-toolkit` and
`python-dotenv` into a local `.venv/`. Subsequent runs are instant. The launcher:

1. Starts CLIProxyAPI if it isn't already running.
2. Starts the cx router if it isn't already running.
3. Opens the model picker, merging CLIProxyAPI's `/v1/models` with your local pool aliases.
4. Launches Claude Code pointed at the router.

Claude Code receives `ANTHROPIC_BASE_URL=http://127.0.0.1:4000` and gateway model discovery
enabled, so it uses whichever model or pool you selected.

## Creating a pool

Manage providers, API keys, and prefixes inside CLIProxyAPI. Press **F8** in Claudex to open
CLIProxyAPI's `management.html`.

Press **F7** in the model picker to create cross-provider pools:

1. Name the pool the way you want Claude Code to see it, e.g. `Opus-level`.
2. Add ≥ 2 provider-specific model IDs (`nvidia_nim/z-ai/glm-5.2`, `vercel/zai/glm-5.2`, ...).
3. Optionally set an RPM per member. If unsure, leave blank (auto).
4. Optionally set a priority per member (lower number = tried first).
5. Pick a routing strategy: `fill-first` (default), `round-robin`, `weighted`, or `least-busy`.
6. Save and return to the picker.

Pool definitions live in `data/pools.json`. The router reloads them automatically on every request
via `mtime`, so edits take effect without restarting anything. A full example of the schema:

```json
{
  "version": 1,
  "pools": [
    {
      "name": "heavy-duty",
      "enabled": true,
      "strategy": "fill-first",
      "members": [
        {"model": "provider/first-model", "rpm": 40, "priority": 0, "limit": 40, "cooldown": 30.0},
        {"model": "provider/second-model", "rpm": 10, "priority": 1}
      ]
    }
  ]
}
```

`rpm` is a *weight* for selection inside a tier, and on its own it is a hard 60-second dispatch
cap: a member with `rpm: 5` will be sent at most 5 requests per trailing 60 s window even if every
other member is busy. Set `limit` explicitly if you want a cap that differs from the weight, and
`cooldown` (seconds) to override the default cool-off after a failed attempt.

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

- **Selection**. Availability is filtered first, in every strategy: members that are cooling are set aside while any member is ready, and a member that has spent its dispatch cap is set aside entirely. A member missing `priority` falls to the *default* tier (`0`); missing `rpm` defaults to `1`. Within the surviving candidates the strategy decides:
  - `fill-first` (default): only members at the lowest priority number are considered, chosen weighted-random by `rpm`. Drains a tier before touching the next.
  - `round-robin`: cycles evenly through the members in config order; priority tiers and `rpm` weights are ignored. The rotation cursor tracks member *identity* and advances exactly once per request, under one atomic lock, parking just past the member the request started with: concurrent requests can never collide on the same starting member, a member skipped while cooling reclaims its own slot once it recovers, and a failover chain does not fling the cursor forward.
  - `weighted`: draws weighted-random by `rpm` across *every* tier at once, so a low-priority member still gets its share of traffic.
  - `least-busy`: picks among the members holding the fewest live in-flight dispatches, breaking ties weighted-random by `rpm`. Best when members differ in latency rather than quota.
- **Pacing**. A member with an explicit `rpm` (or a `limit` override) is *proactively rate-limited*: once that many requests have been sent in the trailing 60 s window, the member leaves the candidate set, so another member serves the request instead of burning a `429`; if every member is paced out the request fails with a sanitized `503` rather than overrunning a cap. The sliding window ages out on its own, so the member rejoins the set when its oldest request falls out of it.
- **Retry**. Pool failover is protocol-driven, not provider-message-driven. A `5xx`, a network error, an SSE `error` **frame envelope**, or a transient `4xx` (`408`/`409`/`425`/`429`) seen before the commit point cools that backend and tries the next member. Any other `4xx` (`400`, `404`, `422`, ...) is terminal: the request itself is wrong, so the provider's own error is forwarded to the client unchanged and no member is cooled. The router reads only envelopes (`event:` and top-level JSON `type`), never assistant text, so a model may safely discuss `event: error`. It sweeps every pool member in priority order, and by default sweeps the list twice (`CX_ROUTER_POOL_PASSES`) before giving up. Capacity-limited providers reject transiently, so a member that failed one sweep often succeeds on the next; a short backoff (1 s, scaling with the sweep number) separates the sweeps so the second pass is not a millisecond replay of the first. Sweeps stop early at the request-wide deadline (180 s, `CX_ROUTER_POOL_TIMEOUT`); each attempt's upstream wait is clamped to what remains of that budget.
- **Empty responses**. A `200` carrying no usable content is a failure, not an answer: zero bytes, no complete SSE frame, an unparseable body, `{}`, an empty content array, or a stream that reaches its terminal event without a single content frame. Any of these cools the member (20 s, `CX_ROUTER_COOLDOWN_EMPTY`) and fails over, so a flaky provider returning empties can no longer stall the client. Each format is judged in its own vocabulary (see **Wire formats** below); a tool-call-only turn counts as content in all three. `count_tokens` is exempt from the content check, because its success body legitimately has none.
- **Cooldown**. `429` honors `Retry-After`; absent that, a member with a per-minute cap uses the short paced-429 cooldown (10 s, `CX_ROUTER_COOLDOWN_PACED_429`) and all other members use 60 s. A per-member `cooldown` overrides either default. A cooldown is an ordering preference rather than a pool-wide outage: if all alternatives are cooling, each is retried before the router returns `503`.
- **Streaming**. The router holds an SSE response until it proves it carries content, which is the first content frame in that format's vocabulary. It then replays the buffered head and streams the rest incrementally via `HTTPResponse.read1()`. Committing therefore costs only the provider's real time-to-first-token, and the client still receives the stream from its very first frame. Once bytes have been forwarded, retrying is unsafe; a later upstream interruption is emitted as a final SSE `error` event, logged, and cools the member so the next request avoids it. Non-streaming responses are buffered only long enough to restore `Content-Length`, allowing clients to distinguish complete and truncated responses.
- **Passthrough**. On a pooled route, if the incoming `model` field is not one of your enabled pool names, the request is forwarded unchanged, but the empty-response check still applies. A single model has no sibling to fail over to, so the equivalent recovery is another attempt at the *same* backend: an unusable `200` is retried up to 3 times (`CX_ROUTER_DIRECT_ATTEMPTS`) with a short backoff, bounded by the same request deadline. Nothing has reached the client at that point, so the retry is invisible; only when every attempt comes back empty does the client see a `502`. Status codes the provider deliberately sent (`4xx`, `429` with its `Retry-After`, `5xx`) are forwarded unchanged and never retried locally. They carry information the client needs, and Claude Code already retries the transient ones itself. Pooled `count_tokens` requests use the same failover policy.
- **Errors**. Non-pooled requests preserve upstream responses unchanged. A pool returns a sanitized `503` only after every member was attempted or the request deadline expired. The response includes a correlation `request_id`; upstream error bodies from pooled attempts are not logged, and only bounded redacted diagnostics record the failure category.

## Endpoints

Pooled routes accept pool names as the `model`, with failover, cooldown, and pacing:
`/v1/messages`, `/v1/messages/count_tokens`, `/v1/responses`, `/v1/chat/completions`.

Passthrough: `/v1/completions`. Plus `/v1/models` (CLIProxyAPI's list + your pool aliases) and
`/health`, `/-/ready`, `/-/health`, `/` for probes.

The pool applies to whichever format the client sent; the router never translates between them.
CLIProxyAPI already serves all three, so a pool member is a *model name*, valid on any of them.

## Wire formats

Failover depends on telling "the model said nothing" apart from "the model is still talking", and
each format spells that differently. The router keeps one grammar per format:

| | `/v1/messages` | `/v1/responses` | `/v1/chat/completions` |
|---|---|---|---|
| Client | Claude Code | Codex | OpenAI-native |
| Content frames | `content_block_start`, `content_block_delta` | `response.output_item.added`, `response.output_text.delta`, `.reasoning_text.delta`, `.reasoning_summary_text.delta`, `.function_call_arguments.delta` | any `delta` with `content`, `reasoning_content`, or `tool_calls` |
| Fail → retry | `event: error` | `event: error`, `response.failed` | `event: error` |
| Terminal with no content → retry | `message_stop` | `response.completed`, `response.incomplete` | end of stream |
| Non-stream body must hold | `content[]` | `output[]` | `choices[]` |

Chat frames carry no `event:` line, so their verdict comes from the delta payload rather than an
event name. Names are read only from the SSE envelope, never from assistant text, so a model may
safely discuss `event: error` without triggering a failover.

## Pointing Codex at a pool

`wire_api = "responses"` is the only value Codex supports, which is why `/v1/responses` is pooled.
In `~/.codex/config.toml`:

```toml
model = "Opus-level"
model_provider = "cx"

[model_providers.cx]
name = "cx-router"
base_url = "http://127.0.0.1:4000/v1"
env_key = "CX_ROUTER_API_KEY"
wire_api = "responses"
```

`model` is a pool name from `data/pools.json`; set `CX_ROUTER_API_KEY` in the environment to the
router key.

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
lines you want to change. The launcher and the router both read it at
startup. A value already present in the real environment (shell export /
launcher) always wins over the file, and unset lines fall back to the built-in
defaults. Real provider credentials live in CLIProxyAPI's `config.yaml`, never
in `.env`; this file only configures local traffic. `.env` is git-ignored.

Router:
- `CX_ROUTER_HOST`, `CX_ROUTER_PORT`, `CX_ROUTER_API_KEY`
- `CX_ROUTER_COOLDOWN_429`, `CX_ROUTER_COOLDOWN_5XX`, `CX_ROUTER_COOLDOWN_NETWORK`,
  `CX_ROUTER_COOLDOWN_AUTH`, `CX_ROUTER_COOLDOWN_PACED_429`, `CX_ROUTER_COOLDOWN_EMPTY`
  (seconds, clamped to 1 through 1800)
- `CX_ROUTER_POOL_TIMEOUT` (seconds, default 180): whole-request budget shared by every
  failover attempt
- `CX_ROUTER_POOL_PASSES` (default 2): sweeps over the member list before a pool gives up
- `CX_ROUTER_DIRECT_ATTEMPTS` (default 3): attempts a single, non-pooled model gets when a `200`
  carries no usable content; `1` disables the retry
- `CX_ROUTER_START_TIMEOUT` (default 35): startup grace period for the router health check
- Legacy aliases still accepted: `CX_LITELLM_HOST`, `CX_LITELLM_PORT`, `CX_LITELLM_API_KEY`

CLIProxyAPI:
- `CX_CLIPROXY_EXE`, `CX_CLIPROXY_CONFIG`
- `CX_CLIPROXY_HOST`, `CX_CLIPROXY_PORT`, `CX_CLIPROXY_API_KEY`
- `CX_CLIPROXY_START_TIMEOUT` (default 15): startup grace period for CLIProxyAPI

Model roles:
- `CX_GPT_FAST_MODEL`, `CX_GPT_MEDIUM_MODEL`, `CX_GPT_SUBAGENT_MODEL`
- `CX_DEFAULT_COMPACT_WINDOW` (default 170000): default auto-compact window

## Files

The data list names common local files. Recovery and temporary artifacts use the patterns shown here.

```
cx.py                       # entry point: orchestrates proxy + router + picker
cx.bat                      # Windows launcher (uv sync + cx.py)
cx.sh                       # Linux/macOS launcher (uv sync + cx.py)
.env.example                # env var template (tracked); copy to .env (ignored)
modules/
  proxy.py                  # CLIProxyAPI lifecycle
  router.py                 # the pool router (messages / responses / chat + passthrough)
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
  .pools.json.lock
  pools.conflict*.json
  .pools.json.*.tmp
  settings.json             # your local picker state (ignored)
  .settings.json.lock
  settings.broken_*.json
  .settings.json.*.tmp
  cli-proxy-api.log         # CLIProxyAPI runtime log (ignored)
  cli-proxy-api.log.1
  cli-proxy-api.lock
  cli-proxy-api.pid         # CLIProxyAPI pid (ignored)
  router.log                # router runtime log (ignored)
  router.log.1
  router.log.2
  router.log.3
  router.boot.log            # router startup output: raw stdout/stderr (ignored)
  router.boot.log.1
  router.lock
  router.log.owner
  router.pid                # router pid (ignored)
tests/
  test_config_errors.py     # invalid environment values and import behavior
  test_cx.py                # launcher loop, wrappers, and malformed pool files
  test_launcher.py          # Claude Code environment setup
  test_models.py            # /v1/models fetch and deduplication
  test_pool_hardening.py    # pool parsing edge cases and atomic writes
  test_pool_tui.py          # pool editor state transitions
  test_pools.py             # pools.json schema and validation
  test_proxy.py             # CLIProxyAPI lifecycle
  test_repository_hygiene.py # repository artifact and inventory gates
  test_router.py            # pool selection, strategies, forwarding, failover, streaming
  test_router_hardening.py  # deadlines, sweeps, empty/truncated bodies, error sanitizing
  test_router_regressions.py # router regression coverage
  test_router_starter.py    # router lifecycle and health checks
  test_tui.py               # picker exit safety and model parameter editing
  test_tui_settings.py      # settings corruption recovery and lost-update protection
```

## Running the tests

```sh
uv run --project . python -m unittest discover -s tests -t .
```

`-t .` sets the top-level directory to the repository root, so the suite is always imported as a
package. That is what installs the test sandbox which keeps the run from writing to `data/`. Without
it, `discover -s tests` makes `tests/` itself the top-level directory, so every test module is
imported as a loose top-level name and `tests/__init__.py`, the only thing that installs the
sandbox, is never imported at all. The risk is silent: `discover -s tests -p test_proxy.py` reports
OK and still deletes `data/cli-proxy-api.pid`. A full `discover -s tests` survives only because
`tests/test_repository_hygiene.py` imports the package as it loads, which installs the guard before
any test runs; that is incidental, so keep `-t .`.

A bare system interpreter may lack `python-dotenv` and prompt-toolkit, so use the
`uv run` form above (or the project `.venv`), not whichever `python` is first on
PATH.

CI runs the same command, plus `ruff check`, on `windows-latest`, `macos-latest`
and `ubuntu-latest` (`.github/workflows/tests.yml`) for every push
to `main` and every pull request. Type checking runs three times, once per
platform (`mypy modules`, then `--platform win32` and `--platform darwin`), because
`mypy` only checks the branches its own platform can reach: a `win32` branch is
invisible to a Linux run, and `darwin` is checked on no runner at all. The suite
is hermetic: it stands up loopback servers on ephemeral ports and never touches
CLIProxyAPI or a provider.

## Multi-session support

The router is a long-lived subprocess (PID in `data/router.pid`, log in `data/router.log`). A
second launcher in another terminal detects the running router and reuses it. Only one CLIProxyAPI
process and one router process are ever needed regardless of how many Claude Code sessions you
have open.

## Logs

- `data/cli-proxy-api.log`: CLIProxyAPI
- `data/router.log`: cx router (per-request selection, cooldowns, retries)
- `data/router.boot.log`: cx router startup output, before its own log is open

Logs are local and ignored. The router records request status and category summaries. Router logs can contain model IDs, filesystem paths, and sanitized error text. Pooled router error bodies are not logged. CLIProxyAPI stdout/stderr is captured in its own local log and may contain provider output. The boot log holds whatever the router process wrote to its stdout and stderr, including raw interpreter tracebacks carrying filesystem paths and model IDs, and it is rotated at 5 MB before the next launch. Review or redact logs before sharing.

## License

Apache 2.0
