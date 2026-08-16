"""Cross-plugin identity tags.

``git.branch``, ``cwd`` and ``entrypoint``, matching the first-party tag
builder, so a hermes generation filters the same way as a cursor or codex one.

Resolved once per process, by reading ``.git`` directly rather than shelling
out to ``git``. The branch can change mid-session, but re-resolving on every
LLM call would put a filesystem walk on the hot path.

The tags reach the backend by two routes with different reach, so they are
split. ``client_tags`` go on the ``ClientConfig`` and the SDK stamps them on
every span as ``agento11y.tag.<key>`` and on the duration metrics as label
dimensions. ``seed_tags`` go on the ``GenerationStart`` and reach the
generations export only. The SDK merges the client tags underneath the seed
ones, so a generation carries the union either way.
"""

from __future__ import annotations

import os
import re

_ENTRYPOINT = "hermes"
_FRAMEWORK_TAGS = {
    "agento11y.framework.name": "hermes",
    "agento11y.framework.source": "plugin",
    "agento11y.framework.language": "python",
}
# Built-ins kept off the client tags. A client tag becomes a label dimension on
# gen_ai.client.operation.duration and the tool duration histogram, and one
# series per working directory is a cardinality bill every user of the plugin
# pays. Anyone who wants it on spans can set it through AGENTO11Y_TAGS.
_SEED_ONLY = frozenset({"cwd"})
# Depth of the walk from cwd towards the filesystem root, matching the
# first-party resolver.
_MAX_PARENTS = 6
_GITDIR_LINE = re.compile(r"^gitdir:\s*(.+)$", re.MULTILINE)
_HEAD_REF = re.compile(r"^ref:\s*refs/heads/(.+)$")
_SHA = re.compile(r"^[0-9a-fA-F]{7,}$")

_CACHED: dict[str, str] | None = None


def builtin_tags() -> dict[str, str]:
    """The built-in tags, resolved on the first call and cached after.

    Keys that cannot be resolved are omitted rather than sent empty.
    """
    global _CACHED
    if _CACHED is None:
        _CACHED = _resolve()
    return dict(_CACHED)


def client_tags() -> dict[str, str]:
    """Tags for the ``ClientConfig``: framework identity plus the cheap built-ins."""
    return {**_FRAMEWORK_TAGS, **{k: v for k, v in builtin_tags().items() if k not in _SEED_ONLY}}


def seed_tags() -> dict[str, str]:
    """Tags for the ``GenerationStart``: the built-ins too expensive to be labels."""
    return {k: v for k, v in builtin_tags().items() if k in _SEED_ONLY}


def _resolve() -> dict[str, str]:
    tags = {"entrypoint": _ENTRYPOINT}
    try:
        cwd = os.getcwd()
    except OSError:
        return tags
    tags["cwd"] = cwd
    branch = _git_branch(cwd)
    if branch:
        tags["git.branch"] = branch
    return tags


def _git_branch(start: str) -> str:
    """Checked-out branch, the first 12 chars of a detached HEAD, or ``""``."""
    git_dir = _find_git_dir(start)
    if not git_dir:
        return ""
    try:
        with open(os.path.join(git_dir, "HEAD"), encoding="utf-8") as handle:
            head = handle.read().strip()
    except OSError:
        return ""
    match = _HEAD_REF.match(head)
    if match:
        return match.group(1).strip()
    return head[:12] if _SHA.match(head) else ""


def _find_git_dir(start: str) -> str:
    current = start
    for _ in range(_MAX_PARENTS):
        resolved = _resolve_git_entry(os.path.join(current, ".git"))
        if resolved:
            return resolved
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return ""


def _resolve_git_entry(path: str) -> str:
    """The git directory ``path`` names: itself, or its ``gitdir:`` target.

    A linked worktree and a submodule both have ``.git`` as a file holding a
    ``gitdir:`` pointer, so HEAD lives elsewhere.
    """
    if os.path.isdir(path):
        return path
    try:
        with open(path, encoding="utf-8") as handle:
            content = handle.read()
    except OSError:
        return ""
    match = _GITDIR_LINE.search(content)
    if not match:
        return ""
    target = match.group(1).strip()
    if not os.path.isabs(target):
        target = os.path.join(os.path.dirname(path), target)
    return os.path.normpath(target)


def _reset_for_tests() -> None:
    global _CACHED
    _CACHED = None
