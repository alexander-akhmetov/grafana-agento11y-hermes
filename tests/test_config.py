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
