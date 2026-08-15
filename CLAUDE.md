# CLAUDE.md

Notes for Claude / contributors working on **grafana-agento11y-hermes**.

## What this is

A Hermes Agent plugin that exports observability data to Grafana Cloud's Agent Observability (agento11y, formerly Sigil). Distributed as a pip package via the `hermes_agent.plugins` entry point under the key `agento11y`. Hermes auto-discovers it, and users opt in via `~/.hermes/config.yaml`. See the README: `hermes plugins enable` does not currently see pip-installed plugins.

## Two channels

The plugin records to two independent destinations, each with its own endpoint and basic-auth pair:

| Channel | Endpoint | What flows | Token scope |
|---|---|---|---|
| Generations | `<cloud>/api/v1/generations:export` | Normalized generation/tool-execution records (the AI Observability UI reads from here) | `sigil:write` |
| OTel | `OTEL_EXPORTER_OTLP_ENDPOINT` (the OTLP HTTP exporters append `/v1/traces` and `/v1/metrics` themselves) | Traces + metrics (`gen_ai.client.*`) | Cloud OTLP write token (set via `OTEL_EXPORTER_OTLP_HEADERS=Authorization=Basic …`) |

Each channel is independently optional. If only one is configured, only that one runs. Generations are configured by `AGENTO11Y_AUTH_TOKEN` (or `AGENTO11Y_AUTH_MODE` non-`none`); OTel is configured by `OTEL_EXPORTER_OTLP_ENDPOINT`.

## Layout

- `__init__.py`: `register(ctx)` wires the hook handlers into Hermes's plugin context and applies the legacy env shim first.
- `_hooks.py`: `pre_api_request` / `post_api_request` (LLM call to generation), `post_tool_call` (tool to tool execution), `on_session_end` (flush). Tags generations with `agento11y.framework.{name,source,language}` so the backend treats us like other framework integrations.
- `_client.py`: lazy singleton `Client`. Build via the SDK's `ClientConfig` plus `GenerationExportConfig(protocol="http", auth=AuthConfig(mode="basic", ...))` when generation creds are set, `protocol="none"` otherwise. Init failure is cached.
- `_otel.py`: installs `TracerProvider` and `MeterProvider` with bare OTLP HTTP exporters (the exporters read `OTEL_EXPORTER_OTLP_*` envs themselves). Each provider is checked independently, so the host can own one and let the plugin install the other. Force-flushes only providers we installed.
- `_config.py`: reads plugin-only knobs under `AGENTO11Y_HERMES_*` and tracks two presence flags, `generations_configured` (from `AGENTO11Y_AUTH_TOKEN` / `AGENTO11Y_AUTH_MODE`) and `otel_configured` (from `OTEL_EXPORTER_OTLP_ENDPOINT`). Channel decisions in `_client.py` and `_otel.py` are driven by these flags.
- `_compat.py`: promotes retired `SIGIL_*` env vars to their `AGENTO11Y_*` names in `os.environ` before anything reads config, then deletes the old key so the SDK does not warn about a var we already honored. Reads the SDK's own `_LEGACY_ENV_RENAMES` so the table cannot drift, and adds the few it omits. Temporary: delete it once the SDK reads the old names itself.
- `_redact.py`: structural payload bounding (depth 4, 50 entries, `AGENTO11Y_HERMES_MAX_CHARS` truncate). No PII regex.
- `_state.py`: `_REQ_STATE` maps `api_request_id` to an open recorder. `_GEN_STATE` and the convo maps are the legacy fallback.

## Hard invariants

- Fail open, always. Every hook handler catches `Exception`, logs at most once, and returns. Telemetry must never block, slow, or crash the Hermes loop. If you add a code path that can raise, wrap it.
- `on_session_end` flushes and does not shut the client down. The `Client` is a process-wide singleton, so shutting it down would break the next session in the same process. Same for the providers we installed.
- OTel auto-setup respects host providers. Only install a provider when the global is the default proxy. Track installed providers separately so `force_flush` only touches ours.
- Use the SDK's exporter for generations: `GenerationExportConfig(protocol="http", auth=AuthConfig(mode="basic", ...))`. Do not hand-roll basic auth or POST loops, because the SDK has retry, batching and queueing built in.
- Do not reinvent OTel env resolution. The OTLP HTTP exporters already read `OTEL_EXPORTER_OTLP_ENDPOINT` (appending `/v1/traces` and `/v1/metrics`), `OTEL_EXPORTER_OTLP_HEADERS`, and `OTEL_EXPORTER_OTLP_INSECURE`. Construct them with no kwargs and let them do the work.

## Hooks contract

Hermes exposes a fixed set of hook names in `hermes_cli/plugins.py:156` (`VALID_HOOKS`). We use:

| Hook | Fires | Where it fires |
|---|---|---|
| `pre_api_request` | per LLM API call, several per turn during tool loops | `agent/conversation_loop.py:2795` |
| `post_api_request` | per LLM API call | `agent/conversation_loop.py:6417` |
| `pre_llm_call` / `post_llm_call` | per turn | `post_llm_call` at `agent/turn_finalizer.py:593` |
| `post_tool_call` | per tool invocation | `model_tools.py` |
| `on_session_end` | per `run_conversation` end and CLI exit | |

All handlers must accept `**kwargs` for forward compatibility.

Hooks we do not register but that exist: `api_request_error`, `on_session_start`, `on_session_finalize`, `on_session_reset`, `subagent_start`, `subagent_stop`, `pre_verify`, the `transform_*` family, and the `on_stream_*` family.

### What the API-request hooks actually carry

Verified against the call sites, not the docs. `hermes_cli/hooks.py` has a `_DEFAULT_PAYLOADS` table that claims to mirror them; it is abbreviated and understates both hooks. Read `agent/conversation_loop.py`.

`pre_api_request` passes `task_id`, `turn_id`, `api_request_id`, `session_id`, `user_message`, `conversation_history`, `request_messages`, `model`, `provider`, `base_url`, `api_mode`, `api_call_count`, `message_count`, `tool_count`, `approx_input_tokens`, `request_char_count`, `max_tokens`, `started_at`, `request`.

`post_api_request` passes `task_id`, `turn_id`, `api_request_id`, `session_id`, `api_duration`, `started_at`, `ended_at`, `finish_reason`, `response_model`, `response`, `usage`, `assistant_message`, `assistant_content_chars`, `assistant_tool_call_count`, `moa_references`.

So the input messages and the assistant message both arrive on the per-call hooks. There is no need to reconstruct output from the turn-level history.

`system_prompt`, `retry_count` and `middleware_trace` also appear on `pre_api_request` at HEAD, but they arrived after v2026.6.5. Do not build on them without raising the floor.

### Pairing a generation to an API call

`api_request_id` is unique per API call and appears on both hooks, so the pair is exact:

- `pre_api_request`: open a recorder, seed `input` from `conversation_history`, store `GenState` in `_state._REQ_STATE` under `api_request_id`.
- `post_api_request`: pop that state, `set_result(input, output=assistant_message, usage, ...)`, close. `completed_at` is pinned to `started_at + api_duration` so the span covers the LLM call.
- `on_session_end`: close anything left open, with empty output.

Each discarded retry has its own `api_request_id`, so each becomes its own generation.

Tool executions are handled entirely in `post_tool_call`, opened and closed in one go.

### LEGACY: hermes older than v2026.6.5

`api_request_id` landed in v2026.6.5 (2026-06-06); v2026.5.29.2 and earlier do not send it. Without it the pre/post pair has to be inferred, so the plugin warns once and falls back to:

- `pre_llm_call` snapshots `conversation_history` into `_CONVO_STATE`, which `post_tool_call` extends with synthesized assistant tool-call and tool-result messages.
- `pre_api_request` keys `GenState` on `(task_id, session_id, api_call_count)`.
- `post_api_request` stashes `usage` and `finish_reason` but leaves the recorder open, because the output is not available yet.
- `post_llm_call` walks the final `conversation_history` and end-anchors the last N assistant messages onto the N pending recorders.

This cannot tell apart two requests running concurrently in one session, which MoA fan-out and subagents produce. Everything marked LEGACY in `_hooks.py` and `_state.py` goes when pre-v2026.6.5 support is dropped.

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

`plugin.yaml` is informational for pip-distributed plugins. Hermes builds the manifest for an entry-point plugin from distribution metadata instead (`hermes_cli/plugins.py`, `discover_entrypoint_manifests`): the name comes from the entry-point name, the version from `dist.version`, the description from the `Summary` field. That is why the file carries no `version:` key, and why a stale one there would never have been read anyway. Shipped for parity with directory plugins and future install-time UX. The `provides_hooks:` key matches the canonical guide.

## Upstream coupling

If Hermes changes hook signatures or adds new lifecycle events, the source of truth is `hermes_cli/plugins.py:VALID_HOOKS` plus the hook reference at `website/docs/user-guide/features/hooks.md` in the upstream `NousResearch/hermes-agent` repo.
