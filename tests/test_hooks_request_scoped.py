"""Request-scoped generation pairing, the path hermes v2026.6.5+ takes.

These tests use the kwarg names hermes actually sends, captured from
``agent/conversation_loop.py`` (``pre_api_request`` at :2795, ``post_api_request``
at :6417). The older tests in ``test_hooks.py`` omit ``api_request_id`` and so
exercise the legacy fallback instead.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from grafana_agento11y_hermes import _hooks, _state

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
