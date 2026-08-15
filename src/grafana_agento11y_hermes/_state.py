"""In-flight recorder state.

Two maps, one per pairing strategy.

``_REQ_STATE`` is the current path. Hermes v2026.6.5 and later pass
``api_request_id`` to both ``pre_api_request`` and ``post_api_request``, which
is a unique id per API call, so the pre/post pair needs no inference.

``_GEN_STATE`` and the convo maps below serve the legacy path, for hermes
builds older than v2026.6.5 that send no ``api_request_id``. There the pair is
inferred from ``(task_id, session_id, api_call_count)``, and output content has
to be recovered later from ``post_llm_call``. Delete everything marked LEGACY
when support for pre-v2026.6.5 hermes is dropped.

Generation state carries the parsed input messages alongside the recorder,
because ``set_result(input=[], output=...)`` would clear the input we seeded.

Tool executions don't need cross-hook state. We only register
``post_tool_call``, do all the work there, and close immediately.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class GenState:
    recorder: Any
    input_messages: list = field(default_factory=list)
    system_prompt: str = ""
    # Set on the current path so a session drain can find this request's state.
    session_id: str = ""
    # LEGACY: partial fields filled in by post_api_request when ``set_result``
    # is deferred to post_llm_call to recover the assistant message. The
    # current path closes in post_api_request and never reads these.
    usage: Any = None
    finish_reason: str = ""
    response_model: str = ""
    # Captured at pre_api_request and post_api_request so the generation span
    # and gen_ai.client.operation.duration metric reflect the LLM call alone,
    # not the close-deferred recorder lifetime that runs through tool execution
    # and any subsequent calls in the same turn.
    started_at: datetime | None = None
    api_duration: float | None = None


# Current path: api_request_id -> state.
_REQ_STATE: dict[str, GenState] = {}
# LEGACY: inferred key for hermes builds without api_request_id.
_GEN_STATE: dict[tuple[str, str, int], GenState] = {}
# Per-(task_id, session_id) running hermes-shaped message list. Populated by
# ``pre_llm_call`` from ``conversation_history`` and extended in-place as
# ``post_tool_call`` fires — so each ``pre_api_request`` snapshot reflects
# the messages going into THIS request, not the start-of-turn snapshot.
_CONVO_STATE: dict[tuple[str, str], list[dict]] = {}
# Count of ``role="assistant"`` messages present in ``conversation_history``
# at ``pre_llm_call`` time. Used by ``_close_pending_for_session`` to slice
# off prior turns' assistants and pair only this turn's outputs to recorders.
# A live count from ``_CONVO_STATE`` is wrong — ``post_tool_call`` extends
# the running convo with synthesized assistant tool-call messages, which we
# don't want included.
_TURN_START_ASST_COUNT: dict[tuple[str, str], int] = {}
# Last (model, provider) seen on a ``pre_api_request``, per session.
# ``post_tool_call`` carries neither, so without this the tool duration metric
# reports an empty ``gen_ai.request.model``.
_SESSION_MODEL: dict[str, tuple[str, str]] = {}
_LOCK = threading.Lock()


def req_put(request_id: str, state: GenState) -> GenState | None:
    """Store state for a request, returning any state it displaced.

    Hermes assigns ``api_request_id`` above its retry loop, so a second
    ``pre_api_request`` for the same id means the first attempt was abandoned.
    The caller closes what it gets back, otherwise that recorder leaks.
    """
    with _LOCK:
        previous = _REQ_STATE.get(request_id)
        _REQ_STATE[request_id] = state
        return previous


def req_pop(request_id: str) -> GenState | None:
    with _LOCK:
        return _REQ_STATE.pop(request_id, None)


def req_pop_session(session_id: str) -> list[GenState]:
    """Pop every request-keyed state for a session, for interrupt cleanup."""
    with _LOCK:
        matching = [(k, v) for k, v in _REQ_STATE.items() if v.session_id == session_id]
        for k, _ in matching:
            del _REQ_STATE[k]
    return [v for _, v in matching]


def gen_put(key: tuple[str, str, int], state: GenState) -> None:
    with _LOCK:
        _GEN_STATE[key] = state


def gen_get(key: tuple[str, str, int]) -> GenState | None:
    with _LOCK:
        return _GEN_STATE.get(key)


def gen_pop(key: tuple[str, str, int]) -> GenState | None:
    with _LOCK:
        return _GEN_STATE.pop(key, None)


def gen_pop_session(session_id: str) -> list[tuple[tuple[str, str, int], GenState]]:
    """Pop and return all GenStates for a session, sorted by api_call_count."""
    with _LOCK:
        matching = [(k, v) for k, v in _GEN_STATE.items() if k[1] == session_id]
        for k, _ in matching:
            del _GEN_STATE[k]
    matching.sort(key=lambda kv: kv[0][2])
    return matching


def convo_set(key: tuple[str, str], messages: list[dict]) -> None:
    with _LOCK:
        _CONVO_STATE[key] = list(messages)


def convo_get(key: tuple[str, str]) -> list[dict]:
    with _LOCK:
        return list(_CONVO_STATE.get(key) or [])


def convo_append(key: tuple[str, str], message: dict) -> None:
    with _LOCK:
        if key in _CONVO_STATE:
            _CONVO_STATE[key].append(message)


def convo_clear(key: tuple[str, str]) -> None:
    with _LOCK:
        _CONVO_STATE.pop(key, None)


def turn_start_asst_count_set(key: tuple[str, str], count: int) -> None:
    with _LOCK:
        _TURN_START_ASST_COUNT[key] = count


def turn_start_asst_count_get(key: tuple[str, str]) -> int | None:
    with _LOCK:
        return _TURN_START_ASST_COUNT.get(key)


def turn_start_asst_count_clear(key: tuple[str, str]) -> None:
    with _LOCK:
        _TURN_START_ASST_COUNT.pop(key, None)


def session_model_put(session_id: str, model: str, provider: str) -> None:
    if not session_id:
        return
    with _LOCK:
        _SESSION_MODEL[session_id] = (model, provider)


def session_model_get(session_id: str) -> tuple[str, str]:
    with _LOCK:
        return _SESSION_MODEL.get(session_id or "", ("", ""))


def session_model_clear(session_id: str) -> None:
    with _LOCK:
        _SESSION_MODEL.pop(session_id, None)


def reset_for_tests() -> None:
    with _LOCK:
        _REQ_STATE.clear()
        _GEN_STATE.clear()
        _CONVO_STATE.clear()
        _TURN_START_ASST_COUNT.clear()
        _SESSION_MODEL.clear()
