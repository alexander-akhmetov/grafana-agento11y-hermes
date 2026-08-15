# grafana-agento11y-hermes

![Grafana Agent Observability UI](img.png)

[Grafana Agent Observability](https://grafana.com/docs/grafana-cloud/machine-learning/ai-observability/) plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent). Records LLM calls and tool executions as generations and emits OTel traces + metrics.

## Install

### Preferred: let your agent do it

Paste this into Hermes (or any Claude / Codex / Cursor / similar agent that can fetch URLs):

```
Install and configure the Grafana Agent Observability plugin for me by following
https://raw.githubusercontent.com/alexander-akhmetov/grafana-agento11y-hermes/main/llms.txt
```

The agent will walk you through pip install, `~/.hermes/config.yaml`, and the credential collection from Grafana Cloud. It will also explain what conversation data flows by default and how to tune it before turning anything on.

### Manual

```bash
pip install git+https://github.com/alexander-akhmetov/grafana-agento11y-hermes
```

Install into the same Python environment hermes runs from (`which hermes` to check). Then enable the plugin in `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - agento11y
```

> Hermes's `plugins enable` CLI does not see pip-installed plugins yet. It only scans `~/.hermes/plugins/` and the bundled directory. Editing the YAML directly is the workaround.

### Hermes version

Use hermes v2026.6.5 or newer. That release added `api_request_id` to the API-request hooks, which is what lets the plugin attribute each generation to the API call it came from.

Older builds still work. The plugin warns once and falls back to matching on a call counter, which cannot tell apart two requests running at the same time in one session, so parallel work (Mixture-of-Agents fan-out, subagents) can attribute output to the wrong generation.

## Upgrading from hermes-plugin-sigil

The package, the module, the entry-point key and the env vars were all renamed. Three steps:

```bash
pip uninstall hermes-plugin-sigil
pip install git+https://github.com/alexander-akhmetov/grafana-agento11y-hermes
```

The uninstall is required. The new package has a different name, so pip installs it alongside the old one instead of replacing it, and both would register a plugin.

Change the key in `~/.hermes/config.yaml` from `sigil` to `agento11y`. The old key no longer resolves, and hermes will not load the plugin without this.

Rename your `SIGIL_*` env vars to `AGENTO11Y_*` (see the table below). The plugin still reads the old names for now and logs what to rename, so nothing breaks the moment you upgrade. The SDK itself ignores them, so this fallback goes away once the SDK is fixed.

## Configure

Two independent channels, each optional: generations under the canonical `AGENTO11Y_*` schema, traces and metrics under the standard OpenTelemetry `OTEL_*` schema. You can find URLs and tokens in your Grafana account: `https://grafana.com/orgs/{org}`.
If you do not have a Grafana Cloud account, you can create one for free at https://grafana.com/auth/sign-up/create-user/. The free tier is enough to run this plugin.

```bash
# Generations → Agent Observability API (Conversations)
export AGENTO11Y_ENDPOINT="https://sigil-prod-<region>.grafana.net"
export AGENTO11Y_PROTOCOL=http
export AGENTO11Y_AUTH_MODE=basic
export AGENTO11Y_AUTH_TENANT_ID="<grafana-cloud-stack-id>"
# Find this token in your stack info → "AI Observability" card at
# https://grafana.com/orgs/{org-id}/stacks/{stack-id}
export AGENTO11Y_AUTH_TOKEN="<sigil:write token>"

# Traces + metrics → Grafana Cloud OTLP gateway (standard OTel envs)
export OTEL_EXPORTER_OTLP_ENDPOINT="https://otlp-gateway-prod-<region>.grafana.net/otlp"
# OTEL_EXPORTER_OTLP_HEADERS is optional: when unset, the plugin derives
# Authorization=Basic base64("$AGENTO11Y_AUTH_TENANT_ID:$AGENTO11Y_AUTH_TOKEN")
# plus X-Scope-OrgID. That only works when the OTLP gateway's basic-auth
# username equals AGENTO11Y_AUTH_TENANT_ID. If the OTLP instance ID differs, set
# it explicitly with that username (override per signal with
# OTEL_EXPORTER_OTLP_TRACES_HEADERS / _METRICS_HEADERS):
# Base64 of "<otlp-instance-id>:<grafana-cloud-otlp-token>" — see your stack's
# "OpenTelemetry" card.
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic <base64>"
```

### Optional

| Variable | Default | Description |
|---|---|---|
| `AGENTO11Y_AGENT_NAME` | `hermes` | Per-generation `gen_ai.agent.name` |
| `OTEL_SERVICE_NAME` | `hermes` | OTel resource `service.name`. Plugin defaults to `hermes` when this and `OTEL_RESOURCE_ATTRIBUTES`'s `service.name` are both unset. |
| `AGENTO11Y_CONTENT_CAPTURE_MODE` | `full` | `full` / `no_tool_content` / `metadata_only`. Plugin defaults to `full` so tool args and results are visible. The SDK's own default is `no_tool_content`, which leaves agent conversations looking empty. |
| `AGENTO11Y_DEBUG` | `false` | Verbose SDK logs |
| `AGENTO11Y_HERMES_SAMPLE_RATE` | `1.0` | Fraction of LLM and tool calls to record, `0.0`–`1.0` |
| `AGENTO11Y_HERMES_MAX_CHARS` | `12000` | Per-string truncation cap for redacted payloads |
| `AGENTO11Y_HERMES_OTEL_AUTO` | `true` | Set `false` if your application already installs a `TracerProvider` / `MeterProvider` |
| `AGENTO11Y_HERMES_AGENT_VERSION` | — | Stamped on each generation as `effective_version`, which tracks per-version drift |

## Verify

```bash
AGENTO11Y_DEBUG=true hermes
```

In `~/.hermes/logs/agent.log` you should see:

```
grafana-agento11y-hermes: installed TracerProvider with OTLP HTTP exporter
grafana-agento11y-hermes: installed MeterProvider with OTLP HTTP exporter
grafana-agento11y-hermes: client initialized (generations=configured, otel=configured)
```

Ask hermes anything, then check **Grafana Cloud -> Observability -> AI -> Conversations**.

## License

Apache-2.0.
