"""Legacy env var promotion.

The SDK ignores the retired names, so ``_compat`` promotes them before any
config is read. These tests drive ``apply_legacy_env`` with an explicit dict,
which also bypasses its once-per-process guard.
"""

from __future__ import annotations

from typing import Any

import pytest

from grafana_agento11y_hermes import _compat


def test_old_name_is_copied_not_moved() -> None:
    """Hermes tool subprocesses inherit the environment, so the old name stays."""
    env = {"SIGIL_AUTH_TOKEN": "glc_secret"}

    promoted = _compat.apply_legacy_env(env)

    assert promoted == ["SIGIL_AUTH_TOKEN"]
    assert env == {"SIGIL_AUTH_TOKEN": "glc_secret", "AGENTO11Y_AUTH_TOKEN": "glc_secret"}


def test_new_name_wins_when_both_are_set() -> None:
    env = {"SIGIL_AUTH_TOKEN": "old", "AGENTO11Y_AUTH_TOKEN": "new"}

    promoted = _compat.apply_legacy_env(env)

    assert promoted == []
    assert env == {"SIGIL_AUTH_TOKEN": "old", "AGENTO11Y_AUTH_TOKEN": "new"}


@pytest.mark.parametrize(
    ("old", "new"),
    [
        # From the SDK's table, including the two whose new name is not just a
        # prefix swap.
        ("SIGIL_API_ENDPOINT", "AGENTO11Y_ENDPOINT"),
        ("SIGIL_TENANT_ID", "AGENTO11Y_AUTH_TENANT_ID"),
        ("SIGIL_REDACT_INPUT_MESSAGES", "AGENTO11Y_REDACT_INPUT_MESSAGES"),
        ("SIGIL_SERVICE_ACCOUNT_TOKEN", "AGENTO11Y_SERVICE_ACCOUNT_TOKEN"),
        # From our own extras, which the SDK's table omits.
        ("SIGIL_DEBUG", "AGENTO11Y_DEBUG"),
        ("SIGIL_HEADERS", "AGENTO11Y_HEADERS"),
        ("SIGIL_HERMES_MAX_CHARS", "AGENTO11Y_HERMES_MAX_CHARS"),
    ],
)
def test_renames_cover_sdk_table_and_our_extras(old: str, new: str) -> None:
    env = {old: "v"}

    _compat.apply_legacy_env(env)

    assert env == {old: "v", new: "v"}


def test_sdk_rename_table_is_reachable() -> None:
    """Guards the private SDK import in ``renames()``.

    If the SDK drops ``_LEGACY_ENV_RENAMES`` we fall back to our extras alone,
    which silently stops promoting credentials. This test fails instead.
    """
    table = _compat.renames()

    assert table["SIGIL_AUTH_TOKEN"] == "AGENTO11Y_AUTH_TOKEN"
    assert len(table) > len(_compat._EXTRA_RENAMES)


def test_our_extras_survive_an_sdk_without_the_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """The private import is the one thing here that a new SDK can take away."""
    import builtins

    real_import = builtins.__import__

    # The parameters are spelled out rather than taken as *args, because the
    # shim is checked against the real ``__import__`` signature.
    def guarded(name: str, globals: Any = None, locals: Any = None, fromlist: Any = (), level: int = 0) -> Any:
        if name == "agento11y.config":
            raise ImportError("no config module")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded)

    table = _compat.renames()

    assert table == _compat._EXTRA_RENAMES
    assert "SIGIL_AUTH_TOKEN" not in table, "the SDK owns that one, and it is gone"


def test_unrelated_names_are_untouched() -> None:
    env = {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://otlp", "HERMES_SIGIL_API_KEY": "stale"}

    promoted = _compat.apply_legacy_env(env)

    assert promoted == []
    assert env == {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://otlp", "HERMES_SIGIL_API_KEY": "stale"}


def test_os_environ_path_runs_once(monkeypatch: pytest.MonkeyPatch) -> None:
    _compat._reset_for_tests()
    monkeypatch.setenv("SIGIL_AUTH_TOKEN", "glc_secret")
    monkeypatch.delenv("AGENTO11Y_AUTH_TOKEN", raising=False)

    first = _compat.apply_legacy_env()

    import os

    assert first == ["SIGIL_AUTH_TOKEN"]
    assert os.environ["AGENTO11Y_AUTH_TOKEN"] == "glc_secret"
    assert os.environ["SIGIL_AUTH_TOKEN"] == "glc_secret"

    # A second call must not re-read the environment.
    monkeypatch.setenv("SIGIL_AUTH_TOKEN", "later")
    assert _compat.apply_legacy_env() == []
    assert os.environ["AGENTO11Y_AUTH_TOKEN"] == "glc_secret"

    _compat._reset_for_tests()
