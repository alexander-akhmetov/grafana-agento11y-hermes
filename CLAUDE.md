# CLAUDE.md

Notes for Claude / contributors working on **grafana-agento11y-hermes**.

## What this is

A Hermes Agent plugin that exports observability data to Grafana Cloud's Agent Observability (agento11y, formerly Sigil). Distributed as a pip package via the `hermes_agent.plugins` entry point under the key `agento11y`. Hermes auto-discovers it, and users opt in via `~/.hermes/config.yaml`. See the README: `hermes plugins enable` does not currently see pip-installed plugins.

## Two channels

The plugin records to two independent destinations, each with its own endpoint and basic-auth pair:

| Channel | Endpoint | What flows | Token scope |
|---|---|---|---|
| Generations | `<cloud>/api/v1/generations:export` | Normalized generation/tool-execution records (the AI Observability UI reads from here) | the setup page token |
| OTel | `OTEL_EXPORTER_OTLP_ENDPOINT` (the OTLP HTTP exporters append `/v1/traces` and `/v1/metrics` themselves) | Traces + metrics (`gen_ai.client.*`) | the same token (set via `OTEL_EXPORTER_OTLP_HEADERS=Authorization=Basic …`) |

Each channel is independently optional. If only one is configured, only that one runs. Generations are configured by `AGENTO11Y_AUTH_TOKEN` (or `AGENTO11Y_AUTH_MODE` non-`none`); OTel is configured by `OTEL_EXPORTER_OTLP_ENDPOINT`.

## Layout

- `__init__.py`: `register(ctx)` wires the hook handlers into Hermes's plugin context and applies the legacy env shim first.
- `_hooks.py`: `pre_api_request` / `post_api_request` (LLM call to generation), `api_request_error` (failed LLM call to a marked generation, then a bounded flush), `post_tool_call` (tool to tool execution), `on_session_end` / `on_session_finalize` (flush). Mints the generation id at pre time, chains the generations of a turn, and starts each tool execution inside the span context of the call that requested it. See "Tags, linking and generation mode".
- `_client.py`: lazy singleton `Client`. Build via the SDK's `ClientConfig` plus `GenerationExportConfig(protocol="http", auth=AuthConfig(mode="basic", ...))` when generation creds are set, `protocol="none"` otherwise. Also carries the identity tags, which reach spans and metrics from here only. Init failure is cached.
- `_otel.py`: installs `TracerProvider` and `MeterProvider` with OTLP HTTP exporters that read the `OTEL_EXPORTER_OTLP_*` envs themselves. The only kwargs passed are the derived auth headers and, for `AGENTO11Y_OTEL_EXPORTER_OTLP_ENDPOINT`, the per-signal endpoint the exporters cannot resolve. Each provider is checked independently, so the host can own one and let the plugin install the other. Force-flushes only providers we installed.
- `_config.py`: reads plugin-only knobs under `AGENTO11Y_HERMES_*` and tracks two presence flags, `generations_configured` (from `AGENTO11Y_AUTH_TOKEN` / `AGENTO11Y_AUTH_MODE`) and `otel_configured` (from `OTEL_EXPORTER_OTLP_ENDPOINT` or its `AGENTO11Y_`-prefixed alias, which `otel_endpoint_override` carries to `_otel.py` because only the standard name reaches the exporters). Channel decisions in `_client.py` and `_otel.py` are driven by these flags.
- `_compat.py`: copies retired `SIGIL_*` env vars to their `AGENTO11Y_*` names in `os.environ` before anything reads config. The old name stays, because hermes spawns subprocesses for tool calls and a telemetry plugin must not change what the host passes to its children; the cost is a duplicate SDK warning. Reads the SDK's own `_LEGACY_ENV_RENAMES` so the table cannot drift, and adds the few it omits. Temporary: delete it once the SDK reads the old names itself.
- `_redact.py`: structural payload bounding (depth 4, 50 entries, `AGENTO11Y_HERMES_MAX_CHARS` truncate). No PII regex.
- `_errors.py`: `ProviderCallError` / `SupersededAttempt`, the sentinels `set_call_error` takes. That method needs a real `Exception`: a string reaches `span.record_exception` and raises inside `end()` under `ContentCaptureMode.FULL`, leaking the span. Both carry `status_code`, which is how the SDK picks `error.category`.
- `_tags.py`: cached `git.branch` / `cwd` / `entrypoint`, matching the first-party tag builder, split into `client_tags()` and `seed_tags()`. Reads `.git/HEAD` directly (following `gitdir:` for worktrees), never shells out, and resolves once per process.
- `_state.py`: `_REQ_STATE` maps `api_request_id` to an open recorder. `_GEN_LINKS` maps it to the generation id and span context, and outlives the recorder, because the tools a call asks for run after it closes. `_TURN_LAST_GEN` maps `turn_id` to the last generation kept in that turn. `_SESSION_MODEL` holds the last model/provider per session, because `post_tool_call` carries neither and the tool duration metric needs `gen_ai.request.model`. `_GEN_STATE` and the convo maps are the legacy fallback.

## Hard invariants

- Fail open, always. Every hook handler catches `Exception`, logs at most once, and returns. Telemetry must never block, slow, or crash the Hermes loop. If you add a code path that can raise, wrap it.
- `on_session_end` flushes and does not shut the client down. The `Client` is a process-wide singleton, so shutting it down would break the next session in the same process. Same for the providers we installed.
- OTel auto-setup respects host providers. Only install a provider when the global is the default proxy. Track installed providers separately so `force_flush` only touches ours.
- Use the SDK's exporter for generations: `GenerationExportConfig(protocol="http", auth=AuthConfig(mode="basic", ...))`. Do not hand-roll basic auth or POST loops, because the SDK has retry, batching and queueing built in.
- Do not reinvent OTel env resolution. The OTLP HTTP exporters already read `OTEL_EXPORTER_OTLP_ENDPOINT` (appending `/v1/traces` and `/v1/metrics`), `OTEL_EXPORTER_OTLP_HEADERS`, and `OTEL_EXPORTER_OTLP_INSECURE`. Construct them with no kwargs and let them do the work.

## Tags, linking and generation mode

### Client tags versus seed tags

Tags reach the backend by two routes with different reach. `ClientConfig.tags` reach everything the SDK emits: the generations export, every span as `agento11y.tag.<key>`, and the duration metrics as label dimensions (`Client._set_client_tag_attributes`, `client.py:1146`). `GenerationStart.tags` reach the generations export alone. `_start_generation` merges the client tags under the seed tags (`client.py:874`), so a generation carries the union either way.

So `_tags.client_tags()` holds the three `agento11y.framework.*` constants plus `entrypoint` and `git.branch`, and `_tags.seed_tags()` holds `cwd` alone. `cwd` stays off the client tags because a client tag becomes a label on `gen_ai.client.operation.duration` and on the tool duration histogram, and one metric series per working directory is a cost every user of the plugin pays. The Go first-party equivalent restricts client tags to user / repo / branch and makes even those opt-in. Anyone who wants `cwd` on spans can add it through `AGENTO11Y_TAGS`, which the SDK merges underneath ours.

### Linking a tool span to the generation that requested it

`ToolExecutionStart` has no `tags`, no `parent_generation_ids` and no `mode` field (`models.py:344`). A tool execution is one OTel span plus one duration histogram sample, and there is no `ToolExecution` message in the ingest proto, so nothing about a tool reaches the generations endpoint. The client tags and the span itself are the only places to put anything.

`pre_api_request` mints the generation id as `gen_<16 hex>` rather than leaving it to the SDK, which only assigns one inside `end()`, where it is too late for a tool to name it. The id and the recorder's span context go into `_state._GEN_LINKS` under `api_request_id`. `post_tool_call` carries the same `api_request_id`, so it finds both: `_start_tool_execution_under` attaches a context holding that span context around `client.start_tool_execution`, and `_stamp_parent_generation` writes `agento11y.generation.parent_generation_ids` on the tool recorder's span.

Two caveats:

- The attribute is speculative. `llms.txt` lists it under the attributes carried by generation and tool spans, but no SDK writes it on an `execute_tool` span and no first-party plugin sets it there. It is one line and removable. The trace parenting is the half with first-party precedent (`plugins/agento11y/internal/emit/emit.go`) and does not depend on it.
- The generation span has already ended when the tool span starts, because the plugin closes it in `post_api_request` and the tools run after that. OTel permits a child of an ended span and the ids are correct, but the child's time range extends past the parent's end. The Go plugins nest while the generation is still open, so their traces do not look like this.

`_GEN_LINKS` outlives the recorder, so it needs its own bound: entries are dropped per session in `on_session_end`, and the map is capped with oldest-first eviction for the sessions that never end.

### Generations of one turn chain

`parent_generation_ids` on a generation names the previous kept generation of the same `turn_id`. That is the DAG the Dependencies tab draws, and a tool loop is genuinely one: call N+1's input is call N's output plus the tool results. `_TURN_LAST_GEN` is written in `on_post_api_request`, at close time rather than open time, so a superseded retry attempt never becomes a parent. It is cleared per turn in `on_post_llm_call`, per session in `on_session_end`, and capped like the link map, because `post_llm_call` does not fire for an interrupted turn.

Chained per turn rather than per session, which is narrower than opencode's per-session chain. MoA fan-out puts concurrent requests in one turn and will chain them in whatever order they close. That is left as it is.

### `mode` stays SYNC

Every generation is `GenerationMode.SYNC`, so `gen_ai.operation.name` is `generateText`, which the UI recognizes. No hook at the 0.16.0 floor carries a per-request streaming signal: hermes copies `api_kwargs` and adds `stream=True` after `pre_api_request` has fired (`agent/auxiliary_client.py`), so the `request` payload does not show it. The `on_stream_*` hooks exist only at hermes HEAD, they dispatch on a worker thread with a bounded queue (`agent/plugin_stream_hooks.py`) so they can lose the race with `post_api_request`, and registering an unknown hook name makes 0.19.0 log a warning listing every valid hook.

The cost of SYNC is the `gen_ai.client.time_to_first_token` histogram, but the mode is only half of what blocks it. The SDK records that histogram when the operation is `streamText` **and** the recorder was given a first-token timestamp (`client.py:1197`), which means a `set_first_token_at` call the plugin has no signal to make: it needs a per-token event, and the only hermes hook that carries one is the HEAD-only `on_stream_*` family. So STREAM alone would rename the operation and still leave the histogram empty. All five Go hook-based first-party plugins use SYNC for the same reason.

## Hooks contract

### Which hermes the line numbers come from

Hermes publishes two version schemes for the same code: git tags `vYYYY.M.D` in `NousResearch/hermes-agent`, and `0.MINOR.PATCH` on PyPI. Users install the PyPI one. Tag `v2026.6.5` (2026-06-05) is PyPI `0.16.0` (2026-06-06), which is this plugin's floor.

Every line number below is from hermes-agent 0.19.0 on PyPI, the release the plugin was last tested against end to end. Differences at upstream HEAD (0.20.1 when this was written) are called out. Line numbers drift with every hermes release. The file and symbol names are the durable part.

Hermes exposes a fixed set of hook names in `hermes_cli/plugins.py`: `VALID_HOOKS`, 23 names at `:135` in 0.19.0, 37 names at `:156` at HEAD. We use:

| Hook | Fires | Where it fires (0.19.0) |
|---|---|---|
| `pre_api_request` | per LLM API call, several per turn during tool loops | `agent/conversation_loop.py:1357` |
| `post_api_request` | once per `api_request_id`, after the retry loop | `agent/conversation_loop.py:4486` |
| `api_request_error` | on some, not all, failed LLM API calls | `run_agent.py:2587` (`_invoke_api_request_error_hook`), called from `agent/conversation_loop.py:1573`, `:1811`, `:2764` |
| `pre_llm_call` / `post_llm_call` | per turn | `post_llm_call` at `agent/turn_finalizer.py:485` |
| `post_tool_call` | per tool invocation | `model_tools.py:974` (`_emit_post_tool_call_hook`) |
| `on_session_end` | per `run_conversation` end, from `finalize_turn`. Not on a turn that returns early | `agent/turn_finalizer.py:616`, plus `cli.py:1261` on interrupt |
| `on_session_finalize` | once at interactive CLI exit. Never in one-shot | `cli.py:7121` |

All handlers must accept `**kwargs` for forward compatibility. `PluginManager.invoke_hook` (`hermes_cli/plugins.py:1912`) injects `telemetry_schema_version` (`hermes.observer.v1`) into every hook payload, so it arrives on hooks whose call site never mentions it.

Hooks that exist and we do not register: `on_session_start`, `on_session_reset`, `subagent_start`, `subagent_stop`, `pre_verify`, `pre_gateway_dispatch`, the approval pair, the `kanban_*` family, and the `transform_*` family. HEAD adds the `on_stream_*` family and `transform_api_error_classification`, which 0.19.0 does not have.

### What the API-request hooks actually carry

Captured from live payloads with a probe plugin on hermes 0.19.0. `hermes_cli/hooks.py` has a `_DEFAULT_PAYLOADS` table that claims to mirror them; it is abbreviated and understates both hooks. Read `agent/conversation_loop.py`.

`pre_api_request` passes `task_id`, `turn_id`, `api_request_id`, `session_id`, `platform`, `user_message`, `conversation_history`, `request_messages`, `model`, `provider`, `base_url`, `api_mode`, `api_call_count`, `message_count`, `tool_count`, `approx_input_tokens`, `request_char_count`, `max_tokens`, `started_at`, `middleware_trace`, `request`, `telemetry_schema_version`.

`post_api_request` passes `task_id`, `turn_id`, `api_request_id`, `session_id`, `platform`, `model`, `provider`, `base_url`, `api_mode`, `api_call_count`, `api_duration`, `started_at`, `ended_at`, `finish_reason`, `message_count`, `response_model`, `response`, `usage`, `assistant_message`, `assistant_content_chars`, `assistant_tool_call_count`, `telemetry_schema_version`.

So the input messages and the assistant message both arrive on the per-call hooks. There is no need to reconstruct output from the turn-level history.

Three kwargs exist only at HEAD, in no released version: `system_prompt` and `retry_count` on `pre_api_request`, and `moa_references` on `post_api_request`. Do not build on them without raising the floor. `middleware_trace` is in 0.19.0 but not in 0.16.0.

Two payload facts to keep in mind when reading `_hooks.py`:

- `conversation_history` carries the turn's messages and no `system` message, so `_split_system_prompt` has nothing to split on the current releases. A system prompt reaches the hooks only through `request`, or through the HEAD-only `system_prompt` kwarg.
- The `request` kwarg, present since the 0.16.0 floor, carries the sanitized provider body: `model`, `messages`, `max_tokens`, `system`, `tools` with full JSON schemas, and `tool_choice`. The `max_tokens` kwarg alongside it arrives as `None`, so the body is the only source for it.

### Pairing a generation to an API call

`api_request_id` appears on both hooks and disambiguates concurrent requests in one session, so the pair is exact:

- `pre_api_request`: open a recorder, seed `input` from `conversation_history`, store `GenState` in `_state._REQ_STATE` under `api_request_id`, and a `GenLink` in `_state._GEN_LINKS` under the same key.
- `post_api_request`: pop that state, `set_result(input, output=assistant_message, usage, ...)`, close. `completed_at` is pinned to `started_at + api_duration` so the span covers the LLM call.
- `api_request_error`: pop that state, close it carrying the provider error.
- `on_session_end`: close anything left open, with empty output.

`api_request_id` is **not** unique per hook invocation. Hermes assigns it at `agent/conversation_loop.py:1224`, above the retry loop at `:1227`, and fires `pre_api_request` inside that loop at `:1357`, so every retry re-fires the hook with the same id. `post_api_request` sits after the loop at `:4486` and fires at most once per id.

So `_state.req_put` returns whatever entry it displaced, and `on_pre_api_request` closes that abandoned attempt with a `SupersededAttempt` call error.

Displacement is a backstop. Every retryable failure exercised against 0.19.0 (429, empty stream, invalid response) fires `api_request_error` first, once per attempt, carrying `retry_count`, `retryable`, `status_code` and an `error` dict with `type` and `message`. The retry loop does hold bare `continue` paths between the two hooks, for compression, context trim and fallback activation, which fire no hook at all. Testing never caught one of those leaving a recorder open, so displacement is unproven; it costs one dict lookup per request, so keep it. Keying on `retry_count` cannot work either, because `post_api_request` never receives it.

Retries that happen *after* `post_api_request` cannot displace anything, because the state is already popped. The incomplete-`REASONING_SCRATCHPAD` retry is one: the check sits at `agent/conversation_loop.py:4547` and its `continue` at `:4555`, both below the hook at `:4486`. Such an attempt is recorded as a normal completed generation carrying the partial content.

Two early returns skip `finalize_turn` (`agent/conversation_loop.py:5784`) and therefore fire no `on_session_end`:

- Retry exhaustion returns at `:4287`. Verified harmless: every attempt was already closed by `api_request_error`, so nothing leaks.
- The thinking-budget-exhausted path returns at `:1947`, straight after `pre_api_request` and before any hook that could close a recorder. Interactive mode closes it on the next turn's `req_pop_session` or at the CLI exit hook. One-shot mode has neither, and it exits through `hermes_cli/main.py` `_exit_after_oneshot`, which calls `os._exit` and so skips the SDK's atexit flush, so an open recorder there has nowhere left to go.

Tool executions are handled entirely in `post_tool_call`, opened and closed in one go. That hook carries `api_request_id` too, which is how the tool span finds the generation that requested it. `status == "error"` becomes `set_exec_error`, called before `set_result`, matching the first-party plugins. `blocked` and `cancelled` stay unrecorded, also matching them.

### LEGACY: hermes older than v2026.6.5 (PyPI 0.16.0)

`api_request_id` landed in tag v2026.6.5, which is PyPI `0.16.0` (2026-06-06); `0.15.2` and earlier do not send it. Checked by unpacking both wheels and reading the `pre_api_request` call site, not inferred from dates. From `0.16.0` on, `post_api_request` also carries everything the current path needs: `assistant_message`, `usage`, `api_duration`, `finish_reason`, `response_model`.

Without `api_request_id` the pre/post pair has to be inferred, so the plugin warns once and falls back to:

- `pre_llm_call` snapshots `conversation_history` into `_CONVO_STATE`, which `post_tool_call` extends with synthesized assistant tool-call and tool-result messages.
- `pre_api_request` keys `GenState` on `(task_id, session_id, api_call_count)`.
- `post_api_request` stashes `usage` and `finish_reason` but leaves the recorder open, because the output is not available yet.
- `post_llm_call` walks the final `conversation_history` and end-anchors the last N assistant messages onto the N pending recorders.

This cannot tell apart two requests running concurrently in one session, which MoA fan-out and subagents produce. Everything marked LEGACY in `_hooks.py` and `_state.py` goes when pre-v2026.6.5 support is dropped.

`_hooks._SAW_REQUEST_ID` is set on the first request that carries an `api_request_id`, and the convo-state writers (`on_pre_llm_call`, the first half of `on_post_tool_call`) return early once it is. The first turn still pays for the bookkeeping, because `pre_llm_call` fires before any `pre_api_request`.

When in doubt about hook kwargs, read the `_invoke_hook(...)` call sites in `agent/` in `NousResearch/hermes-agent`. That is the source of truth, not the docs and not `hermes_cli/hooks.py`.

## Dev workflow

The project is managed with uv. Dev dependencies live in `[dependency-groups]`, not in an extra, so `pip install -e ".[dev]"` no longer works. Use `uv sync`, or `pip install --group dev` on pip 25.1 or newer.

```bash
make sync            # install the venv from uv.lock
make lint            # ruff format --check, ruff check, ty
make test            # pytest on the default Python
make test-all        # pytest on 3.11, 3.12, 3.13 and 3.14
make changelog-test  # the changelog scripts in scripts/
make check           # lint + test + changelog-test
```

`make lint` runs the same three checks as the CI lint job. `make format` rewrites files instead of checking them. `make changelog-test` runs in the CI lint job as well, because the scripts are bash and have nothing to do with the Python matrix.

Commit `uv.lock` with any dependency change. CI runs `uv sync --locked`, which fails when the lock is out of date with `pyproject.toml`.

hatch-vcs derives the version from the git tag, so `pyproject.toml` has no `version` field. A checkout without tags produces a wrong dev version instead of failing, so every workflow uses `fetch-depth: 0`.

Tests stub `agento11y.Client` with `tests/conftest.py:FakeClient` and `_otel.setup_if_needed` with a no-op. The autouse `reset_module_state` fixture clears the cached client + recorder state between tests, and `_otel._reset_for_tests()` shuts down any real providers we installed (otherwise their export threads keep retrying against localhost in the background).

`tests/__init__.py` exists (empty) so `tests/` is an importable package. Three tests do `from tests.conftest import FakeClient` inside `Client` factory lambdas.

`tests/test_hooks.py` omits `api_request_id`, so every case in it exercises the legacy path. `tests/test_hooks_request_scoped.py` covers the current one. Both are needed while the fallback lives.

The suite reads the ambient environment, so exported `AGENTO11Y_*` / `SIGIL_*` values fail the no-credentials cases. Run it clean: `env -i PATH="$PATH" HOME="$HOME" uv run python -m pytest -q`.

## End-to-end testing

The unit tests stub the SDK client and hand-write the hook payloads, so they cannot catch a hermes release changing a hook or a record that never reaches the backend. `.agents/skills/e2e-test/` covers that: it builds a throwaway hermes install with the plugin in it, runs one-shot turns in every configuration, forces provider failures with a scripted mock provider, dumps what each hook really carried, and checks what arrived as generations, spans and metrics. Read `.agents/skills/e2e-test/SKILL.md` before testing a change against a real hermes, and run it on every hermes version bump.

Two of its tools answer questions no amount of reading can. The probe plugin prints the actual hook kwargs, which is the only reliable source for the payload tables above. The local OTLP sink shows what the exporter emitted, which separates a plugin bug from sampling on the receiving end.

## Releasing

Tagging is the whole release. `release.yml` fires on an `X.Y.Z` tag, pre-releases like `0.6.0rc1` and `0.6.0-rc1` included, and runs three jobs:

1. `build` runs the tests again (a tag can point at any commit), builds, checks the built version against the tag, attests provenance, generates the changelog section, creates the GitHub release, and uploads `dist/` and the section as workflow artifacts.
2. `publish-pypi` downloads the `dist` artifact and uploads it to PyPI.
3. `changelog-pr` opens a PR that adds the section to `CHANGELOG.md` on main.

The version check parses both sides with `packaging.version.Version` instead of comparing strings, because hatch-vcs normalizes a `0.6.0-rc1` tag to `0.6.0rc1` in the filename. What it is there to catch is the dev version hatch-vcs produces when HEAD is not exactly on the tag or the tree is dirty.

PyPI goes last of the two publishing steps because it is the only one that cannot be undone. A version number is one-shot: PyPI refuses a re-upload of a version even after you delete it, so a bad release needs a new version, not a retag. A bad GitHub release can be deleted and recreated.

Uploads use PyPI trusted publishing, so no API token exists anywhere. The publisher is bound to this repository, the `release.yml` filename and the `pypi` GitHub environment. Changing any of the three breaks the upload until it is updated at https://pypi.org/manage/account/publishing/.

### Changelog

Ported from kontora, which writes the same kind of plain imperative commit subjects. There is no conventional-commit prefix to group by, so every non-merge subject in the range becomes a bullet.

- `scripts/changelog-for-release.sh <version> [<from-ref>] [<to-ref>]` prints one section and writes nothing. `--no-heading --hashes` is the release-notes form, since the GitHub release title already carries the version.
- `scripts/insert-changelog-section.sh CHANGELOG.md` reads a section on stdin and places it by version order. A version already in the file is a no-op, so re-running the release job cannot stack a duplicate.
- `scripts/backfill-changelog.sh` rebuilds the whole file from every tag. It refuses to run over uncommitted edits, because it is a full rewrite.

Three things the scripts do on purpose. The section date comes from the tagged commit rather than the clock, and the range ends at the tag rather than HEAD, so two checkouts produce identical bytes. The previous tag is the highest one strictly below this release, not the newest tag in the repository, so a backport compares against its own predecessor. Compare links follow the `origin` remote instead of a baked-in URL.

The `Release <tag>: changelog` subject the `changelog-pr` job commits is filtered out of the next release's section. Renaming it in the workflow means renaming it in `BOT_SUBJECTS` too. `DEPENDENCY_SUBJECTS` covers both dependabot shapes, the single `Bump X from Y to Z` and the grouped `Bump the <name> group`, and is deliberately narrow so prose starting with "Bump" stays a normal bullet.

## Plugin manifest note

`plugin.yaml` is informational for pip-distributed plugins. For an entry-point plugin hermes builds the manifest itself and never reads the file. How much it fills in depends on the version. 0.19.0's `_scan_entry_points` (`hermes_cli/plugins.py:1658`) sets only name, source, path and key, so the version and description come out empty. HEAD's `discover_entrypoint_manifests` reads distribution metadata: version from `dist.version`, description from the `Summary` field. Either way the file's own `version:` would never have been read, which is why it carries none. Shipped for parity with directory plugins and future install-time UX. The `provides_hooks:` key matches the canonical guide.

`hermes plugins list` does not show a pip-installed plugin on 0.19.0, and `hermes plugins enable` cannot enable one. The plugin is loaded when `plugins.enabled` in `config.yaml` names it and generations arrive in the UI. Reading the plugin list proves nothing.

## Upstream coupling

If Hermes changes hook signatures or adds new lifecycle events, the source of truth is `hermes_cli/plugins.py:VALID_HOOKS` plus the hook reference at `website/docs/user-guide/features/hooks.md` in the upstream `NousResearch/hermes-agent` repo. Read the release users install from PyPI, not only the git checkout. HEAD carries hook kwargs no released version has, so a claim checked only against the checkout can be wrong for every user.

## Hermes behaviour that bites when debugging an install

- `~/.hermes/.env` is loaded with `override=True` (`hermes_cli/env_loader.py:314`), so a stale `AGENTO11Y_*` or provider key there beats a fresh shell export, with no warning.
- One-shot `-z` calls `logging.disable(logging.CRITICAL)` (`hermes_cli/oneshot.py:198`) right after plugin discovery. Every plugin log record after that point is dropped, including the plugin's own "client initialized" and "installed TracerProvider" lines. Verify an install with interactive `hermes`, where those lines do reach `~/.hermes/logs/agent.log`.
- A shell command that exits non-zero still arrives as `status: "ok"` on `post_tool_call`. `status: "error"` means the tool itself raised, so `set_exec_error` never fires for an ordinary failed command.
- When `execute_tool` spans are missing from Tempo, Adaptive Traces sampling is the usual cause, not the exporter. Point `OTEL_EXPORTER_OTLP_ENDPOINT` at a local OTLP/HTTP sink to see what the plugin really sends before touching the code.
