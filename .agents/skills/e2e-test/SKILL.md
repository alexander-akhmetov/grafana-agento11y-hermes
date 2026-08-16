---
name: e2e-test
description: Test this plugin end to end against a real hermes install - build a throwaway hermes + plugin venv, drive one-shot turns in each configuration (full capture, metadata-only, legacy env names, single-channel, sampling off), force provider failures and retries with a scripted mock provider, dump real hook payloads with a probe plugin, and verify what arrived as generations, spans and metrics. Use when asked to test the plugin end to end, check that telemetry still lands after a change, verify tokens or cost or tool executions or prompt capture, reproduce a failure path, or check the plugin against a new hermes release. The unit tests do not cover any of this - they stub the SDK client and invent hook payloads.
---

# End-to-end tests

`make test` stubs the SDK client and feeds hand-written hook payloads, so it
cannot catch the two things that break in practice: a hermes release changing
what a hook carries, and a record that never reaches the backend. This skill
runs the real thing.

Everything lives under `$E2E_DIR` (default `/tmp/agento11y-hermes-e2e`) with its
own `HERMES_HOME`. Nothing writes to `~/.hermes`.

## What you need

- `uv`, and a provider API key for a cheap model. The default model is
  `claude-haiku-4-5-20251001` on the `anthropic` provider; override with
  `MODEL` / `PROVIDER` and put the matching key in `$E2E_DIR/home/.env`.
- For the backend checks: a Grafana Cloud stack with Agent Observability, the
  `AGENTO11Y_*` / `OTEL_*` block from its setup page, and the `gcx` CLI with a
  context pointing at that stack (`GCX_CONTEXT`).
- Nothing at all for `sink` mode, which exports to a local receiver instead. Use
  it when there is no stack to hand, or to separate "the plugin did not export"
  from "the backend dropped it".

Never paste credentials into a command line or a file in the repo. Point
`AGENTO11Y_ENV_FILE` at the env file you keep outside the repo, and the scripts
read from it.

## Setup

```bash
export E2E_DIR=/tmp/agento11y-hermes-e2e
export ANTHROPIC_API_KEY=...            # or write it to $E2E_DIR/home/.env yourself
export AGENTO11Y_ENV_FILE=/path/to/agento11y-env
export GCX_CONTEXT=<your gcx context>

.agents/skills/e2e-test/scripts/setup.sh          # latest hermes
.agents/skills/e2e-test/scripts/setup.sh 0.19.0   # or pin a version
```

`setup.sh` ends by printing the installed versions and the hooks the plugin
registered. If `hooks registered: NONE`, the plugin is installed but not enabled:
`config.yaml` needs `plugins: {enabled: [agento11y]}`. `hermes plugins list`
never shows a pip-installed plugin, so do not use it to check.

Pin the version the release notes mention when testing against a new hermes, and
run the matrix twice: once on the pinned version, once on the latest.

## The matrix

Each mode stamps its own name as the agent version, so runs are separable in
queries. Force at least one tool call in the prompt, otherwise the tool paths go
untested.

```bash
S=.agents/skills/e2e-test/scripts

$S/run-hermes.sh full "Run 'echo E2E-1' with your terminal tool, then run 'echo E2E-2', then reply with both lines."
$S/run-hermes.sh metadata   "Run 'echo META' with your terminal tool and reply with the output."
$S/run-hermes.sh legacy-env "Reply with exactly: LEGACY-OK"
$S/run-hermes.sh gen-only   "Reply with exactly: GEN-ONLY-OK"
$S/run-hermes.sh otel-only  "Reply with exactly: OTEL-ONLY-OK"
$S/run-hermes.sh none       "Reply with exactly: NO-CHANNEL-OK"
AGENTO11Y_HERMES_SAMPLE_RATE=0 $S/run-hermes.sh full "Reply with exactly: SAMPLED-OUT"
```

What each mode must produce:

| mode | generations | spans | notes |
|---|---|---|---|
| `full` | one per API call | generation + tool spans | content capture defaults to full when the mode env is unset |
| `metadata` | same, text stripped | same, no tool args/results | structure, usage, model and stop reason survive |
| `legacy-env` | yes | yes | retired `SIGIL_*` names only; also proves the branded OTLP alias and credential-derived basic auth |
| `gen-only` | yes, `trace_id` null | none | no OTLP endpoint, so no provider is installed |
| `otel-only` | none | yes | no generation credentials |
| `none` | none | none | one warning naming both channels, and hermes still answers |
| sampling off | none | none | `AGENTO11Y_HERMES_SAMPLE_RATE=0` |

Also run the matrix once from inside a git checkout: `git.branch` only appears
when the cwd is one.

## Verify what arrived

```bash
$S/verify-backend.sh                 # newest hermes-shaped conversation
$S/verify-backend.sh <conversation>  # or a specific one
```

It prints per-generation fields (model, window, stop reason, token types incl.
cache, tags, hermes metadata, input/output part kinds), the agent catalog entry,
the client-side `gen_ai.client.*` metrics, and the backend-computed cost series.
Then fetch a trace and read the attributes:

```bash
gcx --context "$GCX_CONTEXT" traces get <trace-id> -o json > $E2E_DIR/trace.json
python3 $S/show-spans.py $E2E_DIR/trace.json
```

A generation span should carry `gen_ai.request.model`, `gen_ai.response.model`,
the usage attributes, and the finish reason. A tool span should carry
`gen_ai.tool.name`, `gen_ai.tool.call.id`, `gen_ai.request.model` (the plugin
remembers it per session, since the tool hook does not pass one) and, under full
capture, `gen_ai.tool.call.arguments` / `.result`.

Export is asynchronous: wait a few seconds before querying.

### When spans are missing

Sampling on the receiving end drops short internal spans, so a missing
`execute_tool` span is not evidence of a plugin bug. Settle it locally:

```bash
$E2E_DIR/.venv/bin/python $S/otlp-sink.py 8801 &
$S/run-hermes.sh sink "Run 'echo SINK' with your terminal tool then say DONE."
cat $E2E_DIR/otlp-sink.log      # every span and metric the plugin exported
kill %1
```

## Failure and retry paths

The mock provider scripts the responses, so the paths that need a broken
provider are reproducible. It runs under its own `HERMES_HOME`, because
`$HERMES_HOME/.env` overrides the process environment and the real key would
otherwise win.

```bash
$S/run-mock.sh "429,429,ok" "reply with OK"   # retried, then succeeds
$S/run-mock.sh "empty"      "reply with OK"   # retryable fault, retries exhausted
$S/run-mock.sh "401"        "reply with OK"   # not retryable
$S/run-mock.sh "scratchpad" "reply with OK"   # thinking-budget-exhausted path
```

Expected:

- `429,429,ok`: two generations. The failed one carries the provider message,
  `error.type=provider_call_error` and `error.category=rate_limit` on the span,
  span status ERROR with an exception event; the retry's success is its own
  generation. Every attempt re-fires `pre_api_request` under the same
  `api_request_id`, which is the case the request-scoped path exists for.
- `empty`: one generation per attempt, all exported. This also proves the
  bounded flush works, because one-shot exits through `os._exit` and skips the
  SDK's atexit flush.
- `scratchpad`: reaches the path where hermes returns out of the conversation
  loop straight after `pre_api_request`. Read the sequence with
  `show-hooks.py` and note which hooks fire; that set decides what a plugin can
  do here, and it changes between hermes releases.

The mock uses an OpenAI-compatible wire format, so the model shows as
`mock-model` and cost pricing does not apply to those runs.

## Check the hook contract against the running build

`setup.sh` enables the probe plugin, which writes every hook payload:

```bash
python3 $S/show-hooks.py            # $E2E_DIR/hooks.jsonl
```

Read this before trusting any hook documentation, including this repo's. Do it
first on every hermes upgrade. It prints the hook sequence, the kwargs each hook
carried, and the fields the plugin cannot get from a kwarg because they only
exist inside the sanitized request body.

Things worth re-checking there after an upgrade:

- whether `pre_api_request` carries `system_prompt`, and whether
  `conversation_history` contains a `system` role
- whether `max_tokens` arrives as a number or as `None`
- whether `post_tool_call` gained `model` / `provider`, which would retire the
  per-session model memo
- whether `post_api_request` still fires exactly once per `api_request_id`
- new kwargs, which tend to arrive before any documentation does

## Traps

- `$HERMES_HOME/.env` is loaded with override, so it beats the process
  environment. A stale value there silently wins; that is why the scripts build
  the environment with `env -i` and keep only the provider key in `.env`.
- One-shot (`-z`) disables the logging subsystem for the whole run, so plugin
  log lines after plugin discovery reach no log file. Verify log output with an
  interactive hermes session instead.
- The plugin's own test suite reads the ambient environment. Run
  `env -i PATH="$PATH" HOME="$HOME" uv run python -m pytest -q` from the repo, or
  exported `AGENTO11Y_*` / `SIGIL_*` values will fail the no-credentials cases.
- A shell command that exits non-zero still reports `status: ok` on
  `post_tool_call`, so ordinary command failures do not exercise the tool-error
  path.

## Cleanup

```bash
pkill -f mock-provider.py; pkill -f otlp-sink.py
rm -rf "$E2E_DIR"
```

The test data stays in the stack. Runs are tagged with the agent name
(`AGENT_NAME`, default `hermes-e2e`) so they are easy to tell apart from real
traffic.
