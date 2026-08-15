"""Request-scoped generation pairing, the path hermes v2026.6.5+ takes.

These tests use the kwarg names hermes actually sends, captured from
``agent/conversation_loop.py`` (``pre_api_request`` at :2795, ``post_api_request``
at :6417). The older tests in ``test_hooks.py`` omit ``api_request_id`` and so
exercise the legacy fallback instead.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

import pytest

from grafana_agento11y_hermes import _client, _errors, _hooks, _state

CONVO = [
    {"role": "system", "content": "be helpful"},
    {"role": "user", "content": "what is 2+2?"},
]


def texts(messages: Any) -> list[str]:
    """Text of each SDK ``Message``, which stores content as typed parts."""
    return ["".join(p.text for p in m.parts if p.text) for m in messages]


def _pre(client_unused: Any = None, **over: Any) -> None:
    kwargs: dict[str, Any] = {
        "task_id": "task-1",
        "turn_id": "turn-1",
        "api_request_id": "req-1",
        "session_id": "sess-1",
        "user_message": "what is 2+2?",
        "conversation_history": list(CONVO),
        "platform": "cli",
        "model": "claude-sonnet-4-6",
        "provider": "anthropic",
        "api_call_count": 1,
        "message_count": 2,
        "tool_count": 0,
    }
    kwargs.update(over)
    _hooks.on_pre_api_request(**kwargs)


def _post(**over: Any) -> None:
    kwargs: dict[str, Any] = {
        "task_id": "task-1",
        "turn_id": "turn-1",
        "api_request_id": "req-1",
        "session_id": "sess-1",
        "model": "claude-sonnet-4-6",
        "provider": "anthropic",
        "api_call_count": 1,
        "api_duration": 1.5,
        "finish_reason": "stop",
        "response_model": "claude-sonnet-4-6",
        "usage": {"input_tokens": 10, "output_tokens": 4},
        "assistant_message": {"role": "assistant", "content": "4"},
        "assistant_content_chars": 1,
        "assistant_tool_call_count": 0,
    }
    kwargs.update(over)
    _hooks.on_post_api_request(**kwargs)


def test_generation_closes_in_post_api_request(patch_client: Any, env_creds: None) -> None:
    """No post_llm_call needed: the pair is exact and the output is in hand."""
    _pre()
    rec = patch_client._next_gen_recorder
    assert rec is not None and rec.entered
    assert not rec.exited

    _post()

    assert rec.exited
    assert len(rec.set_result_calls) == 1
    call = rec.set_result_calls[0]
    assert call["stop_reason"] == "stop"
    assert call["response_model"] == "claude-sonnet-4-6"


def test_output_comes_from_assistant_message(patch_client: Any, env_creds: None) -> None:
    _pre()
    _post(assistant_message={"role": "assistant", "content": "the answer is 4"})

    call = patch_client._next_gen_recorder.set_result_calls[0]
    assert texts(call["output"]) == ["the answer is 4"]
    assert call["input"], "input seeded at pre-time must survive the close"


def test_completed_at_reflects_api_duration(patch_client: Any, env_creds: None) -> None:
    """Span covers the LLM call, not the recorder lifetime."""
    _pre()
    _post(api_duration=2.0)

    call = patch_client._next_gen_recorder.set_result_calls[0]
    delta = call["completed_at"] - call["started_at"]
    assert delta.total_seconds() == pytest.approx(2.0)


def test_concurrent_requests_in_one_session_do_not_collide(patch_client: Any, env_creds: None) -> None:
    """The bug the legacy api_call_count key cannot avoid.

    Two requests interleave under one task/session with the same api_call_count,
    as MoA fan-out and subagents produce. Each must close its own recorder with
    its own output.
    """
    _pre(api_request_id="req-a", conversation_history=[{"role": "user", "content": "A"}])
    rec_a = patch_client._next_gen_recorder
    _pre(api_request_id="req-b", conversation_history=[{"role": "user", "content": "B"}])
    rec_b = patch_client._next_gen_recorder

    assert rec_a is not rec_b

    _post(api_request_id="req-b", assistant_message={"role": "assistant", "content": "B-out"})
    _post(api_request_id="req-a", assistant_message={"role": "assistant", "content": "A-out"})

    assert texts(rec_a.set_result_calls[0]["output"]) == ["A-out"]
    assert texts(rec_b.set_result_calls[0]["output"]) == ["B-out"]


def test_unmatched_post_is_ignored(patch_client: Any, env_creds: None) -> None:
    _pre(api_request_id="req-1")
    _post(api_request_id="req-does-not-exist")

    assert not patch_client._next_gen_recorder.exited


def test_session_end_closes_a_request_that_never_completed(patch_client: Any, env_creds: None) -> None:
    """Interrupt safety: input and timing still export, output is empty."""
    _pre()
    rec = patch_client._next_gen_recorder

    _hooks.on_session_end(session_id="sess-1")

    assert rec.exited
    assert rec.set_result_calls[0]["output"] == []
    assert rec.set_result_calls[0]["input"]
    assert _state.req_pop("req-1") is None


def test_session_end_only_drains_its_own_session(patch_client: Any, env_creds: None) -> None:
    _pre(api_request_id="req-mine", session_id="sess-1")
    mine = patch_client._next_gen_recorder
    _pre(api_request_id="req-other", session_id="sess-2")
    other = patch_client._next_gen_recorder

    _hooks.on_session_end(session_id="sess-1")

    assert mine.exited
    assert not other.exited


def test_legacy_hermes_warns_once(patch_client: Any, env_creds: None, caplog: pytest.LogCaptureFixture) -> None:
    """No api_request_id means an old hermes, and the user should hear so."""
    with caplog.at_level(logging.WARNING):
        _pre(api_request_id="", conversation_history=None, api_call_count=1)
        _pre(api_request_id="", conversation_history=None, api_call_count=2)

    warnings = [r for r in caplog.records if "api_request_id" in r.getMessage()]
    assert len(warnings) == 1
    assert "v2026.6.5" in warnings[0].getMessage()


def test_a_retry_reusing_the_request_id_closes_the_displaced_recorder(
    patch_client: Any,
    env_creds: None,
) -> None:
    """Hermes assigns api_request_id above its retry loop, so ids repeat."""
    _pre()
    first = patch_client._next_gen_recorder
    _pre()
    second = patch_client._next_gen_recorder

    assert first is not second
    assert first.exited, "the abandoned attempt must not outlive the request"
    assert not second.exited

    _post(assistant_message={"role": "assistant", "content": "kept"})

    assert texts(second.set_result_calls[0]["output"]) == ["kept"]


def test_a_displaced_attempt_is_marked_rather_than_exported_as_a_success(
    patch_client: Any,
    env_creds: None,
) -> None:
    _pre()
    first = patch_client._next_gen_recorder
    _pre()

    assert isinstance(first.set_call_error_calls[0], _errors.SupersededAttempt)
    assert first.set_result_calls[0]["output"] == []
    assert first.set_result_calls[0]["input"], "the attempt's input still exports"


def test_api_request_error_closes_the_attempt_once(patch_client: Any, env_creds: None) -> None:
    _pre()
    rec = patch_client._next_gen_recorder

    _hooks.on_api_request_error(
        api_request_id="req-1",
        error={"type": "RateLimitError", "message": "slow down"},
        status_code=429,
    )

    assert rec.exited
    assert len(rec.set_result_calls) == 1
    assert rec.set_result_calls[0]["call_error"] == "slow down"
    error = rec.set_call_error_calls[0]
    assert isinstance(error, _errors.ProviderCallError)
    assert error.status_code == 429


def test_a_retry_after_the_error_hook_displaces_nothing(patch_client: Any, env_creds: None) -> None:
    _pre()
    failed = patch_client._next_gen_recorder
    _hooks.on_api_request_error(api_request_id="req-1", error="boom", status_code=500)

    _pre()
    retry = patch_client._next_gen_recorder

    assert len(failed.set_call_error_calls) == 1, "closed once, not once per mechanism"
    assert not retry.exited
    assert retry.set_call_error_calls == []


def test_the_status_code_is_read_off_the_error_payload_too(patch_client: Any, env_creds: None) -> None:
    """The hook's status_code kwarg is not the only place hermes could put it."""
    _pre()

    _hooks.on_api_request_error(api_request_id="req-1", error={"message": "slow down", "status_code": 429})

    assert patch_client._next_gen_recorder.set_call_error_calls[0].status_code == 429


def test_api_request_error_for_an_unknown_id_is_a_no_op(patch_client: Any, env_creds: None) -> None:
    _pre()

    _hooks.on_api_request_error(api_request_id="req-other", error="boom")

    assert not patch_client._next_gen_recorder.exited


def test_an_unreadable_error_payload_neither_raises_nor_orphans_the_recorder(
    patch_client: Any,
    env_creds: None,
) -> None:
    """``error`` is whatever hermes built, and reading it must not escape."""

    class Unreadable:
        def __str__(self) -> str:
            raise RuntimeError("unreadable")

    _pre()
    rec = patch_client._next_gen_recorder

    _hooks.on_api_request_error(api_request_id="req-1", error=Unreadable())

    assert not rec.exited, "the state stays in the map for a later sweep"
    _hooks.on_session_end(session_id="sess-1")
    assert rec.exited


def test_an_oversized_error_message_is_truncated(patch_client: Any, env_creds: None) -> None:
    _pre()

    _hooks.on_api_request_error(api_request_id="req-1", error={"message": "x" * 5000})

    message = patch_client._next_gen_recorder.set_result_calls[0]["call_error"]
    assert message.startswith("x" * 2000)
    assert "truncated 3000 chars" in message


def test_the_sdk_derives_the_category_from_our_sentinel() -> None:
    """The status code has to survive the trip into the SDK's classifier.

    ``error.category`` is what a dashboard groups failures by, and the SDK
    reads it off the exception rather than from anything we pass explicitly.
    """
    from agento11y.client import _error_category_from_exception

    cases = {429: "rate_limit", 401: "auth_error", 403: "auth_error", 503: "server_error"}
    for status_code, expected in cases.items():
        error = _errors.ProviderCallError("api_request_error", status_code)
        assert _error_category_from_exception(error, fallback_sdk=True) == expected


def test_a_non_numeric_token_count_does_not_discard_the_generation(
    patch_client: Any,
    env_creds: None,
) -> None:
    """One unusable token value used to abort the close before set_result."""
    _pre()
    _post(usage={"input_tokens": "lots"}, assistant_message={"role": "assistant", "content": "4"})

    call = patch_client._next_gen_recorder.set_result_calls[0]
    assert call["input"]
    assert texts(call["output"]) == ["4"]
    assert call["response_model"] == "claude-sonnet-4-6"
    usage = call["usage"]
    assert usage.input_tokens == 0
    assert usage.output_tokens == 0
    assert usage.total_tokens == 0


@pytest.mark.parametrize(("sent", "recorded"), [(4096, 4096), (None, None), ("8192", 8192), ("none", None)])
def test_max_tokens_is_recorded(patch_client: Any, env_creds: None, sent: Any, recorded: int | None) -> None:
    _pre(max_tokens=sent)

    assert patch_client.start_generation_calls[0].max_tokens == recorded


def test_generations_carry_the_builtin_tags(patch_client: Any, env_creds: None) -> None:
    """The cross-plugin tags, so hermes filters like cursor and codex do."""
    _pre()

    tags = patch_client.start_generation_calls[0].tags
    assert tags["entrypoint"] == "hermes"
    assert tags["cwd"] == os.getcwd()
    # The tests run inside this plugin's own checkout.
    assert tags["git.branch"]
    assert tags["agento11y.framework.name"] == "hermes", "framework tags survive the merge"


def test_effective_version_reads_the_shared_name(patch_client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTO11Y_AGENT_VERSION", "1.2.3")
    _pre()

    assert patch_client.start_generation_calls[0].effective_version == "1.2.3"


def test_the_deprecated_version_name_still_works_and_warns(
    patch_client: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv("AGENTO11Y_AGENT_VERSION", raising=False)
    monkeypatch.setenv("AGENTO11Y_HERMES_AGENT_VERSION", "0.9")

    with caplog.at_level(logging.WARNING):
        _pre()
        _pre(api_request_id="req-2")

    assert patch_client.start_generation_calls[0].effective_version == "0.9"
    deprecations = [r for r in caplog.records if "AGENTO11Y_HERMES_AGENT_VERSION" in r.getMessage()]
    assert len(deprecations) == 1


def test_a_failed_tool_sets_the_exec_error_before_the_result(patch_client: Any, env_creds: None) -> None:
    _hooks.on_post_tool_call(
        tool_name="read_file",
        args={"path": "/nope"},
        result="",
        session_id="sess-1",
        tool_call_id="call-1",
        status="error",
        error_type="FileNotFoundError",
        error_message="boom",
    )

    rec = patch_client._next_tool_recorder
    assert rec.calls == ["set_exec_error", "set_result"]
    assert str(rec.set_exec_error_calls[0]) == "boom"


def test_a_failed_tool_without_a_message_falls_back(patch_client: Any, env_creds: None) -> None:
    _hooks.on_post_tool_call(
        tool_name="read_file",
        session_id="sess-1",
        tool_call_id="call-1",
        status="error",
        error_type="FileNotFoundError",
    )

    assert str(patch_client._next_tool_recorder.set_exec_error_calls[0]) == "FileNotFoundError"


def test_a_successful_tool_sets_no_exec_error(patch_client: Any, env_creds: None) -> None:
    _hooks.on_post_tool_call(
        tool_name="read_file",
        result="contents",
        session_id="sess-1",
        tool_call_id="call-1",
        status="ok",
    )

    assert patch_client._next_tool_recorder.set_exec_error_calls == []


def test_a_tool_execution_carries_the_requesting_model(patch_client: Any, env_creds: None) -> None:
    """post_tool_call carries no model, so the metric needs the cached one."""
    _pre(model="claude-sonnet-4-6", provider="anthropic")

    _hooks.on_post_tool_call(tool_name="read_file", session_id="sess-1", tool_call_id="call-1")

    start = patch_client.start_tool_execution_calls[0]
    assert start.request_model == "claude-sonnet-4-6"
    assert start.request_provider == "anthropic"


def test_current_hermes_stops_maintaining_the_legacy_convo(patch_client: Any, env_creds: None) -> None:
    """Bookkeeping only the pre-v2026.6.5 path reads."""
    _pre()

    _hooks.on_pre_llm_call(session_id="sess-1", conversation_history=list(CONVO))
    _hooks.on_post_tool_call(tool_name="read_file", session_id="sess-1", tool_call_id="call-1")

    assert _state.convo_get(("", "sess-1")) == []


# --- flushing the failure path ---
#
# Hermes one-shot fires no session hook when a turn dies on a provider error,
# and exits via os._exit (hermes_cli/main.py:_exit_after_oneshot), which skips
# the SDK's atexit flush. The error hook is the last chance to export.


def test_api_request_error_flushes(patch_client: Any, env_creds: None) -> None:
    _pre()

    _hooks.on_api_request_error(api_request_id="req-1", error="boom", status_code=500)

    assert patch_client.flush_calls == 1


def test_api_request_error_does_not_flush_for_an_unknown_id(patch_client: Any, env_creds: None) -> None:
    """Nothing was closed, so there is nothing new to export."""
    _pre()

    _hooks.on_api_request_error(api_request_id="req-other", error="boom")

    assert patch_client.flush_calls == 0


def test_error_flush_timeout_zero_skips_the_flush(
    patch_client: Any, env_creds: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _client._get_plugin_config()
    assert cfg is not None
    monkeypatch.setattr(cfg, "error_flush_timeout", 0.0)
    _pre()

    _hooks.on_api_request_error(api_request_id="req-1", error="boom", status_code=500)

    assert patch_client._next_gen_recorder.exited, "the generation still closes"
    assert patch_client.flush_calls == 0


def test_a_hanging_flush_does_not_block_the_hook(
    patch_client: Any, env_creds: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail open: a stuck exporter must not stall the hermes loop."""
    release = threading.Event()

    def hanging_flush() -> None:
        release.wait(30)

    monkeypatch.setattr(patch_client, "flush", hanging_flush)
    cfg = _client._get_plugin_config()
    assert cfg is not None
    monkeypatch.setattr(cfg, "error_flush_timeout", 0.05)
    _pre()

    started = time.monotonic()
    _hooks.on_api_request_error(api_request_id="req-1", error="boom", status_code=500)
    elapsed = time.monotonic() - started
    release.set()

    assert elapsed < 5, f"hook waited {elapsed}s on a hanging flush"


def test_session_finalize_flushes(patch_client: Any, env_creds: None) -> None:
    """The only session hook the interactive failure path gets."""
    _hooks.on_session_finalize(session_id="sess-1")

    assert patch_client.flush_calls == 1


def test_session_finalize_closes_a_still_open_generation(patch_client: Any, env_creds: None) -> None:
    _pre()
    rec = patch_client._next_gen_recorder

    _hooks.on_session_finalize(session_id="sess-1")

    assert rec.exited
