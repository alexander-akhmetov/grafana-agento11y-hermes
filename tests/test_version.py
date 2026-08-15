"""Tests for the generation-export User-Agent token."""

from __future__ import annotations

from grafana_agento11y_hermes import _version


def test_plugin_user_agent_format() -> None:
    ua = _version.plugin_user_agent()
    plugin, sdk = ua.split(" ", 1)
    assert plugin.startswith("agento11y-plugin-hermes/")
    assert plugin.split("/", 1)[1]  # non-empty version
    assert sdk.startswith("agento11y-sdk-python/")
    assert sdk.split("/", 1)[1]


def test_plugin_user_agent_falls_back_when_metadata_missing(monkeypatch) -> None:
    def boom(name: str) -> str:
        raise _version.PackageNotFoundError(name)

    monkeypatch.setattr(_version, "version", boom)
    ua = _version.plugin_user_agent()
    assert ua.startswith("agento11y-plugin-hermes/dev ")
