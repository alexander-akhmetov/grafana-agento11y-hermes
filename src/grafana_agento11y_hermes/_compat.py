"""Reads the retired legacy env vars on behalf of the SDK.

The SDK dropped the old names in the rename to agento11y. It does not read them
and does not fall back: it logs ``<old> is ignored; rename it to <new>`` and
resolves the default instead. An install that still exports the old auth token
therefore loses its credentials, and the plugin decides generations are
unconfigured. Worse for the privacy knobs, where the default is the less
private setting.

This module copies each old name to its new name in ``os.environ`` before any
config is read. The new name always wins when both are set.

The old name stays. Hermes spawns a subprocess for tool calls, and a telemetry
plugin must not change what the host passes to its children. The cost is that
the SDK also warns about a variable we already honored, on top of the rename
notice this module logs.

The rename table comes from the SDK itself, so it cannot drift from what the
SDK actually renamed. ``_EXTRA_RENAMES`` covers the few the SDK's table omits.

Delete this module once the SDK reads the old names itself. Nothing imports it
except ``register()``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import MutableMapping

logger = logging.getLogger(__name__)

# Renames the SDK's own table does not carry. The first two were real settings
# under the old name and are still read under the new one, so the SDK drops them
# silently without even a warning. The rest are this plugin's own knobs.
_EXTRA_RENAMES = {
    "SIGIL_DEBUG": "AGENTO11Y_DEBUG",
    "SIGIL_HEADERS": "AGENTO11Y_HEADERS",
    "SIGIL_HERMES_AGENT_VERSION": "AGENTO11Y_HERMES_AGENT_VERSION",
    "SIGIL_HERMES_MAX_CHARS": "AGENTO11Y_HERMES_MAX_CHARS",
    "SIGIL_HERMES_OTEL_AUTO": "AGENTO11Y_HERMES_OTEL_AUTO",
    "SIGIL_HERMES_SAMPLE_RATE": "AGENTO11Y_HERMES_SAMPLE_RATE",
}

_applied = False


def renames() -> dict[str, str]:
    """Old name -> new name, the SDK's table plus ours.

    Reads a private SDK attribute on purpose: a copy of that table here would
    drift from the SDK's, and the cost of drift is a silently dropped
    credential or privacy setting. Falls back to our own entries when the
    attribute is gone, which is the signal that the SDK now handles this itself.
    """
    table: dict[str, str] = {}
    try:
        from agento11y.config import _LEGACY_ENV_RENAMES

        table.update(_LEGACY_ENV_RENAMES)
    except Exception:
        logger.debug("grafana-agento11y-hermes: SDK legacy rename table unavailable")
    table.update(_EXTRA_RENAMES)
    return table


def apply_legacy_env(env: MutableMapping[str, str] | None = None) -> list[str]:
    """Promote any set legacy var to its ``AGENTO11Y_*`` name.

    Returns the old names that were promoted, for tests. Runs its body once per
    process; later calls return an empty list. Pass ``env`` to act on a dict
    instead of ``os.environ``, which also bypasses the once-only guard.
    """
    global _applied

    target: MutableMapping[str, str]
    if env is None:
        if _applied:
            return []
        _applied = True
        target = os.environ
    else:
        target = env

    promoted = []
    for old, new in renames().items():
        value = target.get(old)
        if value is None:
            continue
        if target.get(new):
            logger.warning(
                "grafana-agento11y-hermes: %s and %s are both set, using %s",
                old,
                new,
                new,
            )
            continue
        target[new] = value
        promoted.append(old)

    if promoted:
        logger.warning(
            "grafana-agento11y-hermes: applied %d renamed env %s for now. Rename %s.",
            len(promoted),
            "var" if len(promoted) == 1 else "vars",
            ", ".join(f"{old} to {new}" for old, new in sorted((o, renames()[o]) for o in promoted)),
        )
    return promoted


def _reset_for_tests() -> None:
    global _applied
    _applied = False
