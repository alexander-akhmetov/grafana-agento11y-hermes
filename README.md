# grafana-agento11y-hermes

[![PyPI](https://img.shields.io/pypi/v/grafana-agento11y-hermes)](https://pypi.org/project/grafana-agento11y-hermes/)

![Grafana Agent Observability UI](https://raw.githubusercontent.com/alexander-akhmetov/grafana-agento11y-hermes/main/img.png)

[Grafana Agent Observability](https://grafana.com/docs/grafana-cloud/machine-learning/ai-observability/) plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent). Records LLM calls and tool executions as generations and emits OTel traces + metrics.

## Install

### Preferred: let your agent do it

Paste this into Hermes (or any Claude / Codex / Cursor / similar agent that can fetch URLs):

```
Install and configure the Grafana Agent Observability plugin for me by following
https://raw.githubusercontent.com/alexander-akhmetov/grafana-agento11y-hermes/main/llms.txt
```

The agent will walk you through pip install, `~/.hermes/config.yaml`, and the credentials from the Agent Observability setup page. It will also explain what conversation data flows by default and how to tune it before turning anything on.

If you do not have a Grafana Cloud account, create one at https://grafana.com/auth/sign-up/create-user/. The free tier is enough.

### Manual

```bash
pip install grafana-agento11y-hermes
```

Install into the same Python environment hermes runs from (`which hermes` to check). Then enable the plugin in `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - agento11y
```

> Hermes's `plugins enable` CLI does not see pip-installed plugins yet. It only scans `~/.hermes/plugins/` and the bundled directory. Editing the YAML directly is the workaround. `hermes plugins list` will not show the plugin either, so check that it works from the data arriving in Grafana Cloud.

<details>
<summary>Upgrading from hermes-plugin-sigil</summary>

The package, the module, the entry-point key and the env vars were all renamed.

1. Reinstall:

```bash
pip uninstall hermes-plugin-sigil
pip install grafana-agento11y-hermes
```

The uninstall is required. The new package has a different name, so pip installs it alongside the old one instead of replacing it, and both would register a plugin.

2. Change the key in `~/.hermes/config.yaml` from `sigil` to `agento11y`. The old key no longer resolves, and hermes will not load the plugin without this.

3. Rename your `SIGIL_*` env vars to `AGENTO11Y_*`, keeping the suffix (`SIGIL_ENDPOINT` becomes `AGENTO11Y_ENDPOINT`). The setup page in Configure below gives you a fresh block with the new names. The plugin still reads the old names for now and logs what to rename, so nothing breaks the moment you upgrade. The SDK itself ignores them, so this fallback goes away once the SDK is fixed.

</details>

## Configure

Everything comes from one page in your stack:

**`https://<stack>.grafana.net/a/grafana-agento11y-app/setup`**

1. Click **Create token**.
2. Click **Copy as environment variables**.
3. Put the block in the environment hermes starts from.

If you keep it in `~/.hermes/.env` instead, hermes loads that file with override on, so a value there beats the same variable exported in your shell, with no warning.

The block you get:

```bash
AGENTO11Y_ENDPOINT=https://agento11y-<...>.grafana.net
AGENTO11Y_PROTOCOL=http
AGENTO11Y_AUTH_MODE=basic
AGENTO11Y_AUTH_TENANT_ID=123456
AGENTO11Y_AUTH_TOKEN=glc_...
OTEL_EXPORTER_OTLP_ENDPOINT=https://otlp-gateway-<...>.grafana.net/otlp
OTEL_EXPORTER_OTLP_HEADERS='Authorization=Basic <base64 of "123456:glc_...">'
```

With no capture mode set, the plugin records full content, so the system prompt, the tool definitions, prompts, assistant replies, tool arguments and tool results all leave the machine; set `AGENTO11Y_CONTENT_CAPTURE_MODE=metadata_only` to keep recording every call while no prompts, responses or tool I/O leave the machine.


## Verify

Use interactive hermes. One-shot mode (`hermes -z`) disables logging for the whole run, so it writes none of the lines below.

```bash
AGENTO11Y_DEBUG=true hermes
```

In `~/.hermes/logs/agent.log` you should see:

```
grafana-agento11y-hermes: installed TracerProvider with OTLP HTTP exporter
grafana-agento11y-hermes: installed MeterProvider with OTLP HTTP exporter
grafana-agento11y-hermes: client initialized (generations=configured, otel=configured)
```

Ask hermes anything, then check **Grafana Cloud -> Observability -> Agent -> Conversations**.

## License

Apache-2.0.
