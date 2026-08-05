

# sigil-hermes

![Grafana AI Observability UI](img.png)

Plugin de [Grafana AI Observability](https://grafana.com/docs/grafana-cloud/machine-learning/ai-observability/) para [Hermes Agent](https://github.com/NousResearch/hermes-agent). Registra las llamadas a LLM y las ejecuciones de herramientas como generaciones de Sigil, y emite trazas y métricas de OTel.

## Instalación

### Opción preferida: deja que tu agente lo haga

Pega esto en Hermes (o en cualquier agente de Claude / Codex / Cursor / similar que pueda acceder a URLs):

```
Install and configure the Grafana AI Observability plugin for me by following
https://raw.githubusercontent.com/alexander-akhmetov/sigil-hermes/main/llms.txt
```

El agente te guiará a través de la instalación con pip, la configuración en `~/.hermes/config.yaml` y la recopilación de credenciales desde Grafana Cloud. También explicará qué datos de conversación se transmiten de forma predeterminada y cómo ajustarlos antes de activar nada.

### Manual

```bash
pip install git+https://github.com/alexander-akhmetov/sigil-hermes
```

Instálalo en el mismo entorno Python desde el que se ejecuta hermes (usa `which hermes` para verificarlo). Luego, habilita el plugin en `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - sigil
```

> La CLI `plugins enable` de Hermes aún no detecta los plugins instalados con pip; solo escanea `~/.hermes/plugins/` y el directorio integrado. Editar el YAML directamente es la solución alternativa.

## Configuración

Dos canales independientes, cada uno opcional: generaciones bajo el esquema canónico `SIGIL_*`, y trazas y métricas bajo el esquema estándar de OpenTelemetry `OTEL_*`. Puedes encontrar las URL y los tokens en tu cuenta de Grafana: `https://grafana.com/orgs/{org}`.
Si no tienes una cuenta de Grafana Cloud, puedes crear una gratis en https://grafana.com/auth/sign-up/create-user/. El nivel gratuito es suficiente para ejecutar este plugin.

```bash
# Generations → Sigil API (Conversations)
export SIGIL_ENDPOINT="https://sigil-prod-<region>.grafana.net"
export SIGIL_PROTOCOL=http
export SIGIL_AUTH_MODE=basic
export SIGIL_AUTH_TENANT_ID="<grafana-cloud-stack-id>"
# Find this token in your stack info → "AI Observability" card at
# https://grafana.com/orgs/{org-id}/stacks/{stack-id}
export SIGIL_AUTH_TOKEN="<sigil:write token>"

# Traces + metrics → Grafana Cloud OTLP gateway (standard OTel envs)
export OTEL_EXPORTER_OTLP_ENDPOINT="https://otlp-gateway-prod-<region>.grafana.net/otlp"
# OTEL_EXPORTER_OTLP_HEADERS is optional: when unset, the plugin derives
# Authorization=Basic base64("$SIGIL_AUTH_TENANT_ID:$SIGIL_AUTH_TOKEN") plus
# X-Scope-OrgID. That only works when the OTLP gateway's basic-auth username
# equals SIGIL_AUTH_TENANT_ID. If the OTLP instance ID differs, set it
# explicitly with that username (override per signal with
# OTEL_EXPORTER_OTLP_TRACES_HEADERS / _METRICS_HEADERS):
# Base64 of "<otlp-instance-id>:<grafana-cloud-otlp-token>" — see your stack's
# "OpenTelemetry" card.
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic <base64>"
```

### Opcional

| Variable | Predeterminado | Descripción |
|---|---|---|
| `SIGIL_AGENT_NAME` | `hermes` | Nombre del agente `gen_ai.agent.name` por generación |
| `OTEL_SERVICE_NAME` | `hermes` | Recurso `service.name` de OTel. El plugin usa `hermes` como predeterminado cuando esta variable y `service.name` de `OTEL_RESOURCE_ATTRIBUTES` no están configuradas. |
| `SIGIL_CONTENT_CAPTURE_MODE` | `full` | `full` / `no_tool_content` / `metadata_only`. El plugin usa `full` como predeterminado para que los argumentos y resultados de las herramientas sean visibles; el predeterminado propio del SDK es `no_tool_content`, lo que deja las conversaciones del agente aparentemente vacías. |
| `SIGIL_DEBUG` | `false` | Registros detallados del SDK |
| `SIGIL_HERMES_SAMPLE_RATE` | `1.0` | Fracción de llamadas a LLM y herramientas a registrar, de `0.0` a `1.0` |
| `SIGIL_HERMES_MAX_CHARS` | `12000` | Límite máximo de caracteres por cadena para truncar cargas útiles redactadas |
| `SIGIL_HERMES_OTEL_AUTO` | `true` | Establece `false` si tu aplicación ya instala un `TracerProvider` / `MeterProvider` |
| `SIGIL_HERMES_AGENT_VERSION` | — | Se marca en cada generación como `effective_version` (Sigil rastrea la deriva por versión) |

## Verificación

```bash
SIGIL_DEBUG=true hermes
```

En `~/.hermes/logs/agent.log` deberías ver:

```
hermes-plugin-sigil: installed TracerProvider with OTLP HTTP exporter
hermes-plugin-sigil: installed MeterProvider with OTLP HTTP exporter
hermes-plugin-sigil: Sigil client initialized (generations=configured, otel=configured)
```

Hazle cualquier consulta a hermes y luego verifica **Grafana Cloud -> Observability -> AI -> Conversations**.

## Licencia

Apache-2.0.
