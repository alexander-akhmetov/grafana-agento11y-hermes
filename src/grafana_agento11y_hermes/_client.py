"""Lazy SDK client construction.

The client is built on first hook invocation. If neither the generations nor
the OTel channel is configured, the plugin is fully no-op. If construction
fails, the failure is cached and handlers never retry.

The SDK's ``Client()`` constructor reads canonical ``AGENTO11Y_*`` env vars
itself, so the plugin leaves transport/auth resolution to it. The override it
supplies is narrow: a generation-export ``User-Agent`` header identifying the
plugin (matching the sibling plugins), ``content_capture=full`` when the user
hasn't picked a mode (overriding the SDK's ``no_tool_content`` default which
hides tool I/O in the UI), and ``protocol="none"`` in OTel-only mode (no
generations creds) to suppress the SDK's HTTP exporter.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

from . import _config, _otel

logger = logging.getLogger(__name__)

_INIT_FAILED = object()
_CLIENT: Any = None
_CONFIG: _config.PluginConfig | None = None
_LOCK = threading.Lock()


def _generation_headers(cfg: _config.PluginConfig) -> dict[str, str]:
    """Headers for the generation export: user ``AGENTO11Y_HEADERS`` plus our User-Agent.

    Setting headers explicitly suppresses the SDK's own ``AGENTO11Y_HEADERS``
    lookup, so we merge that env-derived dict back in. A user-supplied
    ``User-Agent`` (via ``AGENTO11Y_HEADERS``) wins over the plugin default. Auth
    headers are still layered on top by the SDK's resolver.
    """
    from ._version import plugin_user_agent

    headers = dict(cfg.export_headers)
    if not any(key.lower() == "user-agent" for key in headers):
        headers["User-Agent"] = plugin_user_agent()
    return headers


def _to_client_config(cfg: _config.PluginConfig):
    """Build the override ``ClientConfig`` for the SDK.

    Transport/auth stay env-resolved (``endpoint`` / ``protocol`` / ``auth`` are
    left unset). The plugin only layers in:

    - a generation-export ``User-Agent`` header identifying the plugin;
    - ``content_capture=full`` when ``AGENTO11Y_CONTENT_CAPTURE_MODE`` is unset
      (the SDK default ``no_tool_content`` renders agent UIs empty for hermes);
    - ``protocol="none"`` in OTel-only mode so the SDK's HTTP exporter doesn't
      dial the default ingest endpoint.
    """
    from agento11y import ClientConfig, ContentCaptureMode, GenerationExportConfig

    overrides: dict[str, Any] = {}
    if not os.environ.get("AGENTO11Y_CONTENT_CAPTURE_MODE"):
        overrides["content_capture"] = ContentCaptureMode.FULL

    if cfg.generations_configured:
        return ClientConfig(
            generation_export=GenerationExportConfig(headers=_generation_headers(cfg)),
            **overrides,
        )

    return ClientConfig(
        generation_export=GenerationExportConfig(protocol="none"),
        **overrides,
    )


def _get_client(create_if_missing: bool = True) -> Any:
    """Return the cached client or ``None`` if init has failed or cannot run."""
    global _CLIENT, _CONFIG

    if _CLIENT is _INIT_FAILED:
        return None
    if _CLIENT is not None:
        return _CLIENT
    if not create_if_missing:
        return None

    with _LOCK:
        if _CLIENT is _INIT_FAILED:
            return None
        if _CLIENT is not None:
            return _CLIENT

        cfg = _config.load()
        if not (cfg.generations_configured or cfg.otel_configured):
            logger.warning(
                "grafana-agento11y-hermes: no channel configured. Set AGENTO11Y_AUTH_TOKEN "
                "(with AGENTO11Y_ENDPOINT/AGENTO11Y_PROTOCOL/AGENTO11Y_AUTH_*) for generations, "
                "or OTEL_EXPORTER_OTLP_ENDPOINT for traces+metrics. Telemetry disabled."
            )
            _CLIENT = _INIT_FAILED
            return None

        # OTel setup is independent of the SDK client. Fine if it returns False,
        # the generations channel can still work.
        _otel.setup_if_needed(cfg)

        try:
            from agento11y import Client

            override = _to_client_config(cfg)
            _CLIENT = Client() if override is None else Client(override)
            _CONFIG = cfg
            logger.info(
                "grafana-agento11y-hermes: client initialized (generations=%s, otel=%s)",
                "configured" if cfg.generations_configured else "unconfigured",
                "configured" if cfg.otel_configured else "unconfigured",
            )
            return _CLIENT
        except Exception as exc:
            logger.warning("grafana-agento11y-hermes: failed to initialize client: %s", exc)
            _CLIENT = _INIT_FAILED
            return None


def _get_plugin_config() -> _config.PluginConfig | None:
    """Return the resolved plugin config, or ``None`` if the client never initialized."""
    return _CONFIG


def _flush_otel() -> None:
    """Force-flush OTel providers we installed. No-op for host-owned providers."""
    _otel.force_flush()


def _reset_for_tests() -> None:
    """Reset cached client/config state. Test-only."""
    global _CLIENT, _CONFIG
    with _LOCK:
        _CLIENT = None
        _CONFIG = None
    _otel._reset_for_tests()
