"""Config resolution tests, including the OTLP auth-header fallback."""

from __future__ import annotations

import base64

import pytest

from grafana_agento11y_hermes import _config


def _expected_basic(tenant: str, token: str) -> str:
    creds = base64.b64encode(f"{tenant}:{token}".encode()).decode()
    return f"Basic {creds}"


@pytest.fixture(autouse=True)
def _clear_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "AGENTO11Y_AUTH_MODE",
        "AGENTO11Y_AUTH_TENANT_ID",
        "AGENTO11Y_AUTH_TOKEN",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_HEADERS",
        "AGENTO11Y_OTEL_EXPORTER_OTLP_ENDPOINT",
        "AGENTO11Y_HERMES_ERROR_FLUSH_TIMEOUT",
    ):
        monkeypatch.delenv(var, raising=False)


def test_otel_auth_headers_derived_from_generations_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTO11Y_AUTH_TENANT_ID", "stack-1")
    monkeypatch.setenv("AGENTO11Y_AUTH_TOKEN", "glc_secret")
    assert _config._otel_auth_headers() == {
        "Authorization": _expected_basic("stack-1", "glc_secret"),
        "X-Scope-OrgID": "stack-1",
    }


def test_otel_auth_headers_empty_without_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTO11Y_AUTH_TOKEN", "glc_secret")
    assert _config._otel_auth_headers() == {}


def test_otel_auth_headers_empty_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTO11Y_AUTH_TENANT_ID", "stack-1")
    assert _config._otel_auth_headers() == {}


def test_otel_auth_headers_skipped_for_bearer_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bearer token is not a basic password — don't derive basic auth from it."""
    monkeypatch.setenv("AGENTO11Y_AUTH_MODE", "bearer")
    monkeypatch.setenv("AGENTO11Y_AUTH_TENANT_ID", "stack-1")
    monkeypatch.setenv("AGENTO11Y_AUTH_TOKEN", "glc_secret")
    assert _config._otel_auth_headers() == {}


def test_load_populates_otel_auth_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTO11Y_AUTH_TENANT_ID", "stack-1")
    monkeypatch.setenv("AGENTO11Y_AUTH_TOKEN", "glc_secret")
    cfg = _config.load()
    assert cfg.otel_auth_headers == {
        "Authorization": _expected_basic("stack-1", "glc_secret"),
        "X-Scope-OrgID": "stack-1",
    }


def test_load_otel_auth_headers_empty_when_no_creds() -> None:
    assert _config.load().otel_auth_headers == {}


# --- OTLP endpoint resolution ---


def test_otel_unconfigured_without_any_endpoint() -> None:
    cfg = _config.load()
    assert cfg.otel_configured is False
    assert cfg.otel_endpoint_override == ""


def test_standard_endpoint_needs_no_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://otlp.example/otlp")
    cfg = _config.load()
    assert cfg.otel_configured is True
    assert cfg.otel_endpoint_override == ""


def test_branded_endpoint_configures_otel(monkeypatch: pytest.MonkeyPatch) -> None:
    """The branded alias has to turn the OTel channel on by itself."""
    monkeypatch.setenv("AGENTO11Y_OTEL_EXPORTER_OTLP_ENDPOINT", "https://otlp.example/otlp")
    cfg = _config.load()
    assert cfg.otel_configured is True
    assert cfg.otel_endpoint_override == "https://otlp.example/otlp"


def test_standard_endpoint_wins_over_branded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTO11Y_OTEL_EXPORTER_OTLP_ENDPOINT", "https://branded.example/otlp")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://standard.example/otlp")
    cfg = _config.load()
    assert cfg.otel_endpoint_override == "", "the exporters read the standard env themselves"


def test_blank_branded_endpoint_is_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTO11Y_OTEL_EXPORTER_OTLP_ENDPOINT", "   ")
    cfg = _config.load()
    assert cfg.otel_configured is False
    assert cfg.otel_endpoint_override == ""


# --- error flush timeout ---


def test_error_flush_timeout_defaults_to_two_seconds() -> None:
    assert _config.load().error_flush_timeout == 2.0


def test_error_flush_timeout_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTO11Y_HERMES_ERROR_FLUSH_TIMEOUT", "0.5")
    assert _config.load().error_flush_timeout == 0.5


def test_error_flush_timeout_zero_disables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTO11Y_HERMES_ERROR_FLUSH_TIMEOUT", "0")
    assert _config.load().error_flush_timeout == 0.0
