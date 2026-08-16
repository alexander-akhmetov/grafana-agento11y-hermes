"""Identity tag resolution: the ``.git`` walk and the client/seed split.

``_tags`` reads ``.git`` by hand instead of shelling out to git, so every shape
git can leave there is this module's problem: a symbolic HEAD, a detached one, a
worktree or submodule pointer, and no repository at all.
"""

from __future__ import annotations

import os
import pathlib

import pytest

from grafana_agento11y_hermes import _tags


def _repo(root: pathlib.Path, head: str) -> pathlib.Path:
    """A directory holding a ``.git`` directory whose HEAD is ``head``."""
    git_dir = root / ".git"
    git_dir.mkdir(parents=True)
    (git_dir / "HEAD").write_text(head)
    return root


# --- HEAD parsing ---


@pytest.mark.parametrize(
    ("head", "expected"),
    (
        ("ref: refs/heads/main\n", "main"),
        ("ref: refs/heads/feature/nested-name\n", "feature/nested-name"),
        ("ref: refs/heads/main", "main"),
        # Detached HEAD: the short sha stands in for a branch name.
        ("4d1f0c2b9a8e7f60514233aabbccddeeff001122\n", "4d1f0c2b9a8e"),
        # A ref outside refs/heads (a tag checkout) names no branch.
        ("ref: refs/tags/v1.0.0\n", ""),
        ("", ""),
        ("not a head at all\n", ""),
        # Too short to be a sha.
        ("abc123\n", ""),
    ),
)
def test_head_contents_resolve_to_a_branch(tmp_path: pathlib.Path, head: str, expected: str) -> None:
    assert _tags._git_branch(str(_repo(tmp_path, head))) == expected


def test_a_git_dir_without_a_head_file_names_no_branch(tmp_path: pathlib.Path) -> None:
    (tmp_path / ".git").mkdir()
    assert _tags._git_branch(str(tmp_path)) == ""


def test_no_repository_names_no_branch(tmp_path: pathlib.Path) -> None:
    assert _tags._git_branch(str(tmp_path)) == ""


# --- the walk towards the root ---


def test_the_branch_is_found_from_a_subdirectory(tmp_path: pathlib.Path) -> None:
    _repo(tmp_path, "ref: refs/heads/main\n")
    deep = tmp_path / "src" / "pkg"
    deep.mkdir(parents=True)
    assert _tags._git_branch(str(deep)) == "main"


def test_the_walk_stops_after_six_parents(tmp_path: pathlib.Path) -> None:
    """A repository further up than ``_MAX_PARENTS`` is deliberately not found."""
    _repo(tmp_path, "ref: refs/heads/main\n")
    deep = tmp_path.joinpath(*[f"d{i}" for i in range(_tags._MAX_PARENTS)])
    deep.mkdir(parents=True)
    assert _tags._find_git_dir(str(deep)) == ""
    # One level closer is inside the limit.
    assert _tags._find_git_dir(str(deep.parent)) == str(tmp_path / ".git")


def test_the_walk_terminates_at_the_filesystem_root() -> None:
    assert _tags._find_git_dir(os.sep) == ""


# --- worktree and submodule pointers ---


def test_an_absolute_gitdir_pointer_is_followed(tmp_path: pathlib.Path) -> None:
    real = tmp_path / "store" / "worktrees" / "wt1"
    real.mkdir(parents=True)
    (real / "HEAD").write_text("ref: refs/heads/side-branch\n")

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".git").write_text(f"gitdir: {real}\n")

    assert _tags._git_branch(str(checkout)) == "side-branch"


def test_a_relative_gitdir_pointer_resolves_against_the_pointer_file(tmp_path: pathlib.Path) -> None:
    real = tmp_path / "store" / "modules" / "sub"
    real.mkdir(parents=True)
    (real / "HEAD").write_text("ref: refs/heads/submodule-branch\n")

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".git").write_text("gitdir: ../store/modules/sub\n")

    assert _tags._git_branch(str(checkout)) == "submodule-branch"


def test_a_git_file_without_a_pointer_is_ignored(tmp_path: pathlib.Path) -> None:
    (tmp_path / ".git").write_text("this is not a gitdir pointer\n")
    assert _tags._find_git_dir(str(tmp_path)) == ""


def test_an_unreadable_git_entry_is_ignored(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".git").write_text("gitdir: /somewhere\n")

    def deny(*_: object, **__: object) -> object:
        raise PermissionError("nope")

    monkeypatch.setattr("builtins.open", deny)
    assert _tags._resolve_git_entry(str(tmp_path / ".git")) == ""


# --- the resolved tag set ---


def test_tags_carry_cwd_and_branch(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _repo(tmp_path, "ref: refs/heads/main\n")
    monkeypatch.setattr(os, "getcwd", lambda: str(tmp_path))
    assert _tags.builtin_tags() == {
        "entrypoint": "hermes",
        "cwd": str(tmp_path),
        "git.branch": "main",
    }


def test_an_unresolvable_branch_is_omitted_rather_than_sent_empty(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "getcwd", lambda: str(tmp_path))
    assert "git.branch" not in _tags.builtin_tags()


def test_an_unresolvable_cwd_leaves_only_the_entrypoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deleted working directory makes getcwd raise; the tags still resolve."""

    def deny() -> str:
        raise OSError("cwd is gone")

    monkeypatch.setattr(os, "getcwd", deny)
    assert _tags.builtin_tags() == {"entrypoint": "hermes"}


def test_resolution_is_cached_after_the_first_call(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def counting_getcwd() -> str:
        calls.append(1)
        return str(tmp_path)

    monkeypatch.setattr(os, "getcwd", counting_getcwd)
    _tags.builtin_tags()
    _tags.builtin_tags()
    assert len(calls) == 1, "the .git walk must stay off the per-request path"


def test_cwd_is_a_seed_tag_and_never_a_client_tag(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A client tag becomes a metric label, and one series per cwd is a bill."""
    _repo(tmp_path, "ref: refs/heads/main\n")
    monkeypatch.setattr(os, "getcwd", lambda: str(tmp_path))

    client, seed = _tags.client_tags(), _tags.seed_tags()
    assert seed == {"cwd": str(tmp_path)}
    assert "cwd" not in client
    assert client == {
        "agento11y.framework.name": "hermes",
        "agento11y.framework.source": "plugin",
        "agento11y.framework.language": "python",
        "entrypoint": "hermes",
        "git.branch": "main",
    }


def test_the_cached_dict_cannot_be_mutated_by_a_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    tags = _tags.builtin_tags()
    tags["entrypoint"] = "tampered"
    assert _tags.builtin_tags()["entrypoint"] == "hermes"
