"""Hermes plugin hook handlers.

All handlers fail open: any exception is caught, logged at most once per kind,
and the hermes loop is allowed to continue. If the SDK client cannot be
constructed (missing creds, SDK error), every handler short-circuits via
``client is None``.
"""

from __future__ import annotations

import json
import logging
import os
import random
from datetime import UTC, datetime, timedelta
from typing import Any

from . import _client, _redact, _state

logger = logging.getLogger(__name__)

# LEGACY: cleared only by the test reset. Delete with the rest of the
# pre-v2026.6.5 fallback.
_WARNED_LEGACY_HERMES = False


def _warn_legacy_hermes_once() -> None:
    """Warn that hermes is too old to pair requests exactly.

    hermes v2026.6.5 added ``api_request_id`` and the input/output messages to
    the API-request hooks. Older builds fall back to matching on
    ``api_call_count`` and recovering output from ``post_llm_call``, which
    cannot tell apart two requests running concurrently in one session.
    """
    global _WARNED_LEGACY_HERMES
    if _WARNED_LEGACY_HERMES:
        return
    _WARNED_LEGACY_HERMES = True
    logger.warning(
        "grafana-agento11y-hermes: this hermes does not send api_request_id. "
        "Using the legacy matching path, which mis-attributes concurrent "
        "requests in one session. Upgrade to hermes v2026.6.5 or newer."
    )


def _reset_for_tests() -> None:
    global _WARNED_LEGACY_HERMES
    _WARNED_LEGACY_HERMES = False


def _agent_name() -> str:
    """Default agent name when the SDK can't resolve one from env/context.

    The SDK reads ``AGENTO11Y_AGENT_NAME`` itself; this fallback only kicks in
    when neither env nor a context override is set.
    """
    return os.environ.get("AGENTO11Y_AGENT_NAME", "").strip() or "hermes"


def _convo_key(task_id: str, session_id: str) -> tuple[str, str]:
    """Key for the running conversation history.

    Hermes does not pass ``task_id`` to ``pre_llm_call`` but does to
    ``pre_api_request`` — keying on session_id only is the only way both hooks
    address the same bucket. Wrapped in a tuple so the type matches what
    ``_state`` expects.
    """
    return ("", session_id or "")


def _should_sample() -> bool:
    """Return True if this trace should be recorded under AGENTO11Y_HERMES_SAMPLE_RATE.

    A pre-hook that returns False simply skips ``start_generation`` and
    never stores a recorder, so the matching post-hook becomes a natural
    no-op (``gen_pop`` returns None). Tool sampling is checked at
    ``post_tool_call`` time directly.
    """
    cfg = _client._get_plugin_config()
    if cfg is None or cfg.sample_rate >= 1.0:
        return True
    if cfg.sample_rate <= 0.0:
        return False
    return random.random() < cfg.sample_rate


def _coerce_text(content: Any) -> str:
    """Best-effort conversion of a hermes message ``content`` field to a string.

    Hermes mirrors OpenAI/Anthropic shapes — content can be a string or a list
    of typed blocks (``{"type": "text", "text": "..."}``). We collapse list
    blocks into newline-joined text.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for block in content:
            if isinstance(block, str):
                chunks.append(block)
            elif isinstance(block, dict):
                if isinstance(block.get("text"), str):
                    chunks.append(block["text"])
                elif isinstance(block.get("content"), str):
                    chunks.append(block["content"])
                else:
                    chunks.append(json.dumps(block, default=str))
            else:
                chunks.append(repr(block))
        return "\n".join(c for c in chunks if c)
    return str(content)


def _split_system_prompt(messages: Any) -> tuple[str, list[dict]]:
    """Pull system messages out into a single prompt string, return remaining messages."""
    if not isinstance(messages, list):
        return "", []
    system_parts: list[str] = []
    rest: list[dict] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "system":
            text = _coerce_text(msg.get("content"))
            if text:
                system_parts.append(text)
        else:
            rest.append(msg)
    return "\n\n".join(system_parts), rest


def _serialize_tool_calls(tool_calls: Any) -> list[dict[str, Any]]:
    if not tool_calls:
        return []
    out: list[dict[str, Any]] = []
    for tc in tool_calls:
        if isinstance(tc, dict):
            tc_id = tc.get("id", "")
            fn = tc.get("function") or {}
            name = fn.get("name") if isinstance(fn, dict) else None
            arguments = fn.get("arguments") if isinstance(fn, dict) else None
        else:
            tc_id = getattr(tc, "id", "")
            fn = getattr(tc, "function", None)
            name = getattr(fn, "name", None) if fn is not None else None
            arguments = getattr(fn, "arguments", None) if fn is not None else None
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except Exception:
                pass
        out.append({"id": tc_id or "", "name": name or "", "arguments": arguments})
    return out


def _to_sdk_message(msg: dict[str, Any]):
    """Convert one hermes-shaped message dict to a agento11y.Message."""
    from agento11y import (
        Message,
        MessageRole,
        Part,
        ToolCall,
        ToolResult,
        text_part,
        tool_call_part,
        tool_result_part,
    )

    role = msg.get("role")
    if role == "user":
        text = _coerce_text(msg.get("content"))
        return Message(role=MessageRole.USER, parts=[text_part(text)] if text else [])
    if role == "tool":
        tool_call_id = msg.get("tool_call_id") or ""
        content = _coerce_text(msg.get("content"))
        return Message(
            role=MessageRole.TOOL,
            parts=[tool_result_part(ToolResult(tool_call_id=tool_call_id, content=content))],
        )
    if role == "assistant":
        parts: list[Part] = []
        text = _coerce_text(msg.get("content"))
        if text:
            parts.append(text_part(text))
        for tc in _serialize_tool_calls(msg.get("tool_calls")):
            input_json = b""
            if tc.get("arguments") is not None:
                try:
                    input_json = json.dumps(tc["arguments"]).encode()
                except Exception:
                    input_json = b""
            parts.append(tool_call_part(ToolCall(name=tc.get("name", ""), id=tc.get("id", ""), input_json=input_json)))
        return Message(role=MessageRole.ASSISTANT, parts=parts)
    # Unknown role (e.g. "system" should already be filtered out): drop.
    return None


def _to_sdk_messages(messages: Any) -> list:
    if not isinstance(messages, list):
        return []
    out = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "system":
            continue  # handled via system_prompt
        sdk_msg = _to_sdk_message(msg)
        if sdk_msg is not None:
            out.append(sdk_msg)
    return out


def _assistant_to_sdk_messages(assistant_message: Any) -> list:
    """Build a single-element list[Message] from a hermes assistant response."""
    if assistant_message is None:
        return []

    if isinstance(assistant_message, dict):
        content = assistant_message.get("content")
        tool_calls = assistant_message.get("tool_calls")
    else:
        content = getattr(assistant_message, "content", None)
        tool_calls = getattr(assistant_message, "tool_calls", None)

    msg_dict = {"role": "assistant", "content": content, "tool_calls": tool_calls}
    sdk_msg = _to_sdk_message(msg_dict)
    return [sdk_msg] if sdk_msg is not None else []


def _build_token_usage(usage: Any):
    from agento11y import TokenUsage

    if not isinstance(usage, dict):
        return TokenUsage()

    input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    total_tokens = int(usage.get("total_tokens") or 0)
    cache_read = int(usage.get("cache_read_tokens") or usage.get("cache_read_input_tokens") or 0)
    cache_write = int(
        usage.get("cache_write_tokens")
        or usage.get("cache_creation_input_tokens")
        or usage.get("cache_write_input_tokens")
        or 0
    )
    reasoning = int(usage.get("reasoning_tokens") or 0)

    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cache_read_input_tokens=cache_read,
        cache_write_input_tokens=cache_write,
        reasoning_tokens=reasoning,
    )


def on_pre_llm_call(
    *,
    task_id: str = "",
    session_id: str = "",
    conversation_history: Any = None,
    user_message: Any = None,
    **_: Any,
) -> None:
    """Capture the start-of-turn conversation so request-scoped hooks have an input.

    Hermes does not pass ``messages`` to ``pre_api_request``. ``pre_llm_call``
    is the only hook that receives the actual conversation as
    ``conversation_history`` (the message list at the start of the turn).
    We snapshot it here and extend in-place as the tool-calling loop runs.
    """
    if not isinstance(conversation_history, list):
        return
    convo = list(conversation_history)
    if (
        isinstance(user_message, str)
        and user_message
        and not any(isinstance(m, dict) and m.get("role") == "user" and m.get("content") == user_message for m in convo)
    ):
        convo.append({"role": "user", "content": user_message})
    key = _convo_key(task_id, session_id)
    _state.convo_set(key, convo)
    # Snapshot the assistant-message count BEFORE post_tool_call extends the
    # running convo with synthesized tool-call messages. ``_close_pending_for_session``
    # uses this to peel this turn's assistant outputs off the final history.
    start_asst_count = sum(1 for m in conversation_history if isinstance(m, dict) and m.get("role") == "assistant")
    _state.turn_start_asst_count_set(key, start_asst_count)


def on_post_llm_call(
    *,
    task_id: str = "",
    session_id: str = "",
    conversation_history: Any = None,
    assistant_response: Any = None,
    **_: Any,
) -> None:
    """Close all pending recorders for this turn with outputs from the final convo.

    ``conversation_history`` here is the FINAL state of the turn — includes
    every assistant message (with content/tool_calls) and every tool result.
    We pair the new assistant messages with our pending recorders in order.
    """
    _close_pending_for_session(session_id or "", conversation_history)
    key = _convo_key(task_id, session_id)
    _state.convo_clear(key)
    _state.turn_start_asst_count_clear(key)


def on_pre_api_request(
    *,
    task_id: str = "",
    session_id: str = "",
    model: str = "",
    provider: str = "",
    conversation_history: Any = None,
    api_request_id: str = "",
    turn_id: str = "",
    messages: Any = None,
    api_call_count: int = 0,
    **_: Any,
) -> None:
    client = _client._get_client()
    if client is None:
        return
    if not _should_sample():
        return

    try:
        from agento11y import GenerationStart, ModelRef

        # hermes v2026.6.5+ passes the input messages here as
        # ``conversation_history`` (agent/conversation_loop.py). ``messages`` is
        # accepted only because older builds and our own tests used that name.
        if not isinstance(conversation_history, list):
            conversation_history = messages
        if not isinstance(conversation_history, list):
            # LEGACY: no messages on the hook, so use the running history that
            # pre_llm_call captured and post_tool_call extends.
            _warn_legacy_hermes_once()
            conversation_history = _state.convo_get(_convo_key(task_id, session_id))
        system_prompt, non_system = _split_system_prompt(conversation_history)
        sdk_messages = _to_sdk_messages(non_system)

        # Stamp started_at on both seed and GenState. The seed timestamp is
        # what the SDK uses for the span's start_time; GenState carries it so
        # the close path can compute completed_at = started_at + api_duration.
        started_at = datetime.now(UTC)
        start = GenerationStart(
            model=ModelRef(provider=provider or "unknown", name=model or "unknown"),
            conversation_id=session_id or task_id or "",
            agent_name=_agent_name(),
            effective_version=os.environ.get("AGENTO11Y_HERMES_AGENT_VERSION", "").strip(),
            system_prompt=system_prompt,
            started_at=started_at,
            tags={
                "agento11y.framework.name": "hermes",
                "agento11y.framework.source": "plugin",
                "agento11y.framework.language": "python",
            },
            metadata={
                "hermes.api_call_count": api_call_count,
                "hermes.task_id": task_id,
                "hermes.session_id": session_id,
                "hermes.turn_id": turn_id,
            },
        )
        recorder = client.start_generation(start)
        recorder.__enter__()
        # Input is stashed on GenState and threaded into set_result at
        # close-time, so set_result is only called once.
        state = _state.GenState(
            recorder=recorder,
            input_messages=sdk_messages,
            system_prompt=system_prompt,
            session_id=session_id,
            started_at=started_at,
        )
        if api_request_id:
            _state.req_put(api_request_id, state)
        else:
            # LEGACY: infer the pair from the call counter.
            _warn_legacy_hermes_once()
            _state.gen_put((task_id, session_id, int(api_call_count or 0)), state)
    except Exception as exc:
        logger.warning("grafana-agento11y-hermes: on_pre_api_request failed: %s", exc)


def on_post_api_request(
    *,
    task_id: str = "",
    session_id: str = "",
    api_call_count: int = 0,
    api_request_id: str = "",
    model: str = "",
    usage: Any = None,
    finish_reason: str = "",
    response_model: str = "",
    assistant_message: Any = None,
    api_duration: float | None = None,
    **_: Any,
) -> None:
    """Close the generation for this API call.

    hermes v2026.6.5+ passes ``api_request_id`` and ``assistant_message`` here
    (agent/conversation_loop.py), so the pair is exact and the output is in
    hand: set the result and close immediately. Each discarded retry is its own
    request id, so each becomes its own generation.

    LEGACY: without ``api_request_id`` the output is not available yet, so only
    the partial fields are stashed and the close is deferred to post_llm_call,
    which is the first hook carrying the assistant content.
    """
    if api_request_id:
        state = _state.req_pop(api_request_id)
        if state is None:
            return
        _finish_generation(
            state,
            assistant_message=assistant_message,
            usage=usage,
            finish_reason=finish_reason,
            response_model=response_model or model or "",
            api_duration=api_duration,
        )
        return

    state = _state.gen_get((task_id, session_id, int(api_call_count or 0)))
    if state is None:
        return
    state.usage = usage
    state.finish_reason = finish_reason or ""
    state.response_model = response_model or model or ""
    if isinstance(api_duration, (int, float)) and api_duration >= 0:
        state.api_duration = float(api_duration)


def _finish_generation(
    state: Any,
    *,
    assistant_message: Any,
    usage: Any,
    finish_reason: str,
    response_model: str,
    api_duration: float | None,
) -> None:
    """Set the result on one recorder and close it.

    Pins ``completed_at`` to ``started_at + api_duration`` when hermes gave us a
    duration, so the span and the ``gen_ai.client.operation.duration`` metric
    cover the LLM call rather than the recorder's lifetime. That matters on the
    legacy path, where the close can happen long after the call.
    """
    recorder = state.recorder
    try:
        sdk_output = _assistant_to_sdk_messages(assistant_message) if assistant_message is not None else []
        duration = api_duration if api_duration is not None else state.api_duration
        completed_at: datetime | None = None
        if state.started_at is not None and duration is not None:
            completed_at = state.started_at + timedelta(seconds=duration)
        recorder.set_result(
            input=state.input_messages,
            output=sdk_output,
            usage=_build_token_usage(usage),
            stop_reason=finish_reason or "",
            response_model=response_model or "",
            started_at=state.started_at,
            completed_at=completed_at,
        )
    except Exception as exc:
        logger.warning("grafana-agento11y-hermes: set_result failed: %s", exc)
    try:
        recorder.__exit__(None, None, None)
    except Exception as exc:
        logger.warning("grafana-agento11y-hermes: recorder __exit__ failed: %s", exc)


def _close_pending_for_session(session_id: str, conversation_history: Any) -> None:
    """LEGACY: drain deferred recorders, assigning outputs from the final convo.

    Only reachable on hermes older than v2026.6.5, which sends no
    ``api_request_id``. Called from ``post_llm_call`` (normal path) and
    ``on_session_end`` (interrupt safety). Walks the new portion of
    ``conversation_history`` to find assistant messages in order and pairs them
    with stored GenStates by api_call_count.
    """
    pending = _state.gen_pop_session(session_id or "")
    if not pending:
        return

    asst_messages: list[dict] = []
    if isinstance(conversation_history, list):
        for msg in conversation_history:
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                asst_messages.append(msg)

    # Slice off prior turns' assistants using the count snapshotted at
    # pre_llm_call time. Falling back to 0 keeps tests that skip pre_llm_call
    # working — they pass single-turn histories where everything is "new".
    start_count = _state.turn_start_asst_count_get(_convo_key("", session_id or "")) or 0
    new_asst = asst_messages[start_count:]

    # End-anchor: pair the LAST n_new pending recorders with the n_new new
    # assistant messages. Hermes increments api_call_count on every iteration,
    # including discarded retries (incomplete <REASONING_SCRATCHPAD>, invalid-
    # response retries — see run_agent.py:12944, 11428), so pending can have
    # more entries than there are kept assistants. Anchoring from the end is
    # correct because post_llm_call only fires when ``final_response`` is set
    # (run_agent.py:13771), so the LAST iteration was always kept; leading
    # discards leave their recorder with no output rather than stealing a
    # message from a successful call or a prior turn.
    n_new = len(new_asst)
    pair_offset = max(0, len(pending) - n_new)

    for idx, ((_, _, _api_call_count), gen_state) in enumerate(pending):
        new_idx = idx - pair_offset
        asst = new_asst[new_idx] if 0 <= new_idx < n_new else None
        _finish_generation(
            gen_state,
            assistant_message=asst,
            usage=gen_state.usage,
            finish_reason=gen_state.finish_reason,
            response_model=gen_state.response_model,
            api_duration=None,
        )


def on_post_tool_call(
    *,
    tool_name: str = "",
    args: Any = None,
    result: Any = None,
    task_id: str = "",
    session_id: str = "",
    tool_call_id: str = "",
    duration_ms: int | None = None,
    **_: Any,
) -> None:
    """Record the tool execution and extend the running convo for the next call.

    All work is done here, not split with pre_tool_call. Current hermes invokes
    pre_tool_call from ``run_agent.py:9060`` and ``run_agent.py:9520`` without
    ``session_id`` / ``tool_call_id`` (they default to ``""`` in
    ``get_pre_tool_call_block_message``), but post_tool_call (``model_tools.py:732``)
    always carries the real ids. Doing everything in post avoids a key mismatch
    between the two hooks that would leak recorders and misroute convo state.

    Order: append the synthesized assistant tool-call message, then the tool
    result, so the next ``pre_api_request``'s input chain reads
    ``user → assistant(tool_calls) → tool``. Then start, set_result, and close
    the tool execution recorder, using ``duration_ms`` from hermes to backdate
    the span's started_at so its duration reflects the tool's wallclock time.
    """
    convo_key = _convo_key(task_id, session_id)
    if tool_call_id:
        try:
            args_str = json.dumps(args) if args is not None else "{}"
        except Exception:
            args_str = "{}"
        _state.convo_append(
            convo_key,
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": tool_call_id,
                        "type": "function",
                        "function": {"name": tool_name or "", "arguments": args_str},
                    }
                ],
            },
        )
        try:
            content = result if isinstance(result, str) else json.dumps(result, default=str)
        except Exception:
            content = repr(result)
        _state.convo_append(
            convo_key,
            {"role": "tool", "tool_call_id": tool_call_id, "content": content},
        )

    client = _client._get_client()
    if client is None:
        return
    if not _should_sample():
        return

    try:
        from agento11y import ToolExecutionStart

        completed_at = datetime.now(UTC)
        if isinstance(duration_ms, (int, float)) and duration_ms >= 0:
            started_at = completed_at - timedelta(milliseconds=float(duration_ms))
        else:
            started_at = completed_at

        # Leave include_content at its default. The SDK resolves tool-content
        # capture from the mode: forced on under full, forced off under
        # metadata_only / full_with_metadata_spans, and the seed is honored
        # under no_tool_content. Pinning it True kept args/results in the span
        # even when the user set no_tool_content.
        start = ToolExecutionStart(
            tool_name=tool_name or "",
            tool_call_id=tool_call_id or "",
            conversation_id=session_id or task_id or "",
            agent_name=_agent_name(),
            started_at=started_at,
        )
        recorder = client.start_tool_execution(start)
        recorder.__enter__()
        cfg = _client._get_plugin_config()
        # cfg is non-None here: _get_client() above only returns a client after
        # _CONFIG was populated by `_client._get_client()`.
        max_chars = cfg.max_chars if cfg is not None else 12000
        try:
            try:
                recorder.set_result(
                    arguments=_redact.safe_value(args, max_chars=max_chars, parse_json_strings=True),
                    result=_redact.safe_value(result, max_chars=max_chars, parse_json_strings=True),
                    completed_at=completed_at,
                )
            except Exception as exc:
                logger.warning("grafana-agento11y-hermes: tool set_result failed: %s", exc)
        finally:
            try:
                recorder.__exit__(None, None, None)
            except Exception as exc:
                logger.warning("grafana-agento11y-hermes: tool recorder __exit__ failed: %s", exc)
    except Exception as exc:
        logger.warning("grafana-agento11y-hermes: on_post_tool_call failed: %s", exc)


def on_session_end(*, session_id: str = "", **_: Any) -> None:
    # Interrupt safety. A request whose post_api_request never fired (the user
    # interrupted, or the call errored into api_request_error) would leak its
    # recorder. Close it with the input and timing we already have; there is no
    # output to recover.
    if session_id:
        for state in _state.req_pop_session(session_id):
            _finish_generation(
                state,
                assistant_message=None,
                usage=None,
                finish_reason="",
                response_model="",
                api_duration=None,
            )

    # LEGACY: same safety for the deferred path, where post_llm_call only fires
    # on a successful turn (agent/turn_finalizer.py:593 ``if final_response and
    # not interrupted``).
    if session_id:
        _close_pending_for_session(session_id, None)

    # flush() leaves the singleton client open so subsequent hermes sessions
    # in the same process keep working. shutdown() would set _closed=True and
    # every future start_generation/start_tool_execution call would raise.
    client = _client._get_client(create_if_missing=False)
    if client is not None:
        try:
            client.flush()
        except Exception as exc:
            logger.warning("grafana-agento11y-hermes: client.flush failed: %s", exc)
    # Flush the BatchSpanProcessor we installed — Client.flush() drains the
    # SDK's JSON export channel, but the OTel pipeline is independent.
    _client._flush_otel()
