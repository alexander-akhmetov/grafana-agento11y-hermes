"""Check one exported conversation against what the plugin promises to record.

Reads the JSON of `agento11y conversations get -o json` and reports, per
generation, whether each recorded field arrived. Reports rather than asserts:
what is expected depends on the capture mode, the channels in use and the hermes
version, and the point is to see the whole picture in one place after a run.

usage: check-generations.py <conversation.json>
"""

from __future__ import annotations

import json
import sys


def part_kinds(messages: list) -> list[str]:
    kinds = []
    for message in messages or []:
        role = str(message.get("role", "")).replace("MESSAGE_ROLE_", "").lower()
        for part in message.get("parts") or []:
            kind = next(iter(part), "?") if part else "empty"
            kinds.append(f"{role}:{kind}")
    return kinds


def mark(ok: bool) -> str:
    return "yes" if ok else "NO"


document = json.load(open(sys.argv[1]))
generations = document.get("generations") or []
print(f"generations: {len(generations)}")

for generation in generations:
    usage = generation.get("usage") or {}
    tags = generation.get("tags") or {}
    metadata = generation.get("metadata") or {}
    estimate = generation.get("context_token_estimate") or {}
    print(f"\n--- {generation.get('generation_id')}")
    print(f"  model            {generation.get('model')} response_model={generation.get('response_model')!r}")
    print(f"  agent            {generation.get('agent_name')} version={generation.get('agent_version')!r}")
    print(f"  capture mode     {metadata.get('agento11y.sdk.content_capture_mode')}")
    print(f"  window           {generation.get('started_at')} -> {generation.get('completed_at')}")
    print(f"  stop_reason      {generation.get('stop_reason')!r}")
    print(f"  error            {json.dumps(generation.get('error'))}")
    print(f"  trace linked     {mark(bool(generation.get('trace_id')))} ({generation.get('trace_id')})")
    print(f"  input parts      {part_kinds(generation.get('input'))}")
    print(f"  output parts     {part_kinds(generation.get('output'))}")
    print("  tokens           " + ", ".join(f"{k}={v}" for k, v in sorted(usage.items())) or "  tokens           none")
    print(f"  cache tokens     {mark(any('cache' in k for k in usage))}")
    print(f"  framework tags   {mark(tags.get('agento11y.framework.name') == 'hermes')}")
    print(f"  cwd/entrypoint   {mark('cwd' in tags and 'entrypoint' in tags)}")
    print(f"  git.branch       {mark('git.branch' in tags)} (only set when the cwd is a git checkout)")
    print(f"  hermes metadata  {mark(all(f'hermes.{k}' in metadata for k in ('task_id', 'session_id', 'turn_id')))}")
    print(f"  system_prompt    {mark(bool(generation.get('system_prompt')))}")
    print(f"  tools recorded   {len(generation.get('tools') or [])}")
    print(f"  max_tokens       {generation.get('max_tokens')}")
    print(f"  token estimate   system_prompt={estimate.get('system_prompt')} tools_total={estimate.get('tools_total')}")
