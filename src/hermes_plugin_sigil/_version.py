"""Plugin identity for the generation-export User-Agent.

The sibling Sigil plugins (claude-code, codex, copilot, cursor, pi) tag their
generation exports with a most-specific-first User-Agent so Sigil can attribute
ingest traffic to the integration. We match that convention:

    sigil-plugin-hermes/<plugin-version> sigil-sdk-python/<sdk-version>
"""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

_PLUGIN_PRODUCT = "sigil-plugin-hermes"
_SDK_PRODUCT = "sigil-sdk-python"


def _plugin_version() -> str:
    try:
        return version("hermes-plugin-sigil")
    except PackageNotFoundError:
        return "dev"


def _sdk_user_agent() -> str:
    """SDK product token, e.g. ``sigil-sdk-python/0.5.0``.

    Newer SDKs expose this directly; older builds don't, so we fall back to
    package metadata using the same format the SDK uses.
    """
    try:
        from sigil_sdk.version import user_agent

        return user_agent()
    except Exception:
        try:
            return f"{_SDK_PRODUCT}/{version('sigil-sdk')}"
        except PackageNotFoundError:
            return f"{_SDK_PRODUCT}/unknown"


def plugin_user_agent() -> str:
    """Return the generation-export User-Agent for this plugin."""
    return f"{_PLUGIN_PRODUCT}/{_plugin_version()} {_sdk_user_agent()}"
