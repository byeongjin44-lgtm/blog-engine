"""Claude API client. Structured output uses `output_config.format` (JSON schema outputs).

Docs: https://platform.claude.com/docs/en/build-with-claude/structured-outputs
"""

from __future__ import annotations

import copy
import json
import os
from typing import Any, Callable

import time

from engine.errors import is_transient, owner_action_for_anthropic
from engine.http import HttpError, request_json

RETRY_WAITS = (20, 60, 120)  # seconds between attempts on transient errors

API_URL = "https://api.anthropic.com/v1/messages"

class TruncatedError(RuntimeError):
    """The answer hit max_tokens before the JSON was complete."""


# Injected in tests: (payload) -> response dict.
Transport = Callable[[dict], dict]


def _default_transport(payload: dict, sleep=time.sleep) -> dict:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    for attempt in range(len(RETRY_WAITS) + 1):
        try:
            return request_json(
                "POST",
                API_URL,
                payload,
                headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                timeout=600,
            )
        except HttpError as e:
            owner = owner_action_for_anthropic(e)
            if owner:
                raise owner from None
            if not is_transient(e) or attempt == len(RETRY_WAITS):
                raise
            print(f"Claude API busy ({e.status}), retrying in {RETRY_WAITS[attempt]}s")
        except Exception as e:  # noqa: BLE001
            if not is_transient(e) or attempt == len(RETRY_WAITS):
                raise
            print(f"Network error ({e}), retrying in {RETRY_WAITS[attempt]}s")
        sleep(RETRY_WAITS[attempt])
    raise AssertionError("unreachable")


def strict_schema(schema: dict) -> dict:
    """JSON outputs require additionalProperties: false on every object."""
    schema = copy.deepcopy(schema)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object":
                node["additionalProperties"] = False
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    return schema


# Claude 5.x models think before answering by default (adaptive thinking), and thinking tokens count toward
# max_tokens and are billed as output. Sonnet 5.5 defaults to "high" effort, Haiku 5.5 to "medium".
# A blog post does not need deep thinking: "low" effort keeps cost down and stops posts being cut off.
# Docs: https://platform.claude.com/docs/en/build-with-claude/effort
DEFAULT_EFFORT = "low"


def thinking_off(model: str) -> dict | None:
    """The lowest thinking setting a model accepts (no up-front thinking), or None if unknown."""
    if "sonnet" in model:
        return {"type": "between_tools"}
    if "haiku" in model:
        return {"type": "disabled"}
    return None


def structured_call(
    *,
    model: str,
    system: str,
    prompt: str,
    schema: dict,
    max_tokens: int = 8000,
    transport: Transport | None = None,
    effort: str | None = DEFAULT_EFFORT,
    no_thinking: bool = False,
) -> dict[str, Any]:
    """Ask Claude for a JSON object matching `schema`; returns it parsed."""
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
        "output_config": {"format": {"type": "json_schema", "schema": strict_schema(schema)}},
    }
    if effort:
        payload["output_config"]["effort"] = effort
    if no_thinking and thinking_off(model):
        payload["thinking"] = thinking_off(model)
    send = transport or _default_transport
    try:
        response = send(payload)
    except HttpError as e:
        # Self-healing: if the API rejects the thinking/effort settings, send the plain request once.
        body = (e.body or "").lower()
        if e.status != 400 or not ("effort" in body or "thinking" in body):
            raise
        print(f"::warning title=Claude 설정 거부::{model}: effort/thinking 설정 없이 다시 보냄 ({e.body[:200]})")
        payload.pop("thinking", None)
        payload["output_config"].pop("effort", None)
        response = send(payload)
    stop = response.get("stop_reason")
    if stop == "max_tokens":
        cap = response.get("_max_tokens", max_tokens)  # CostGuard may have lowered it to fit the budget
        raise TruncatedError(f"Claude response was cut off at {cap} tokens"
                             + (" (output limited by the per-post budget)" if cap < max_tokens else ""))
    if stop == "refusal":
        raise RuntimeError("Claude declined to write this content")
    text = "".join(b.get("text", "") for b in response.get("content", []) if b.get("type") == "text")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Claude returned invalid JSON ({e}): {text[:300]}") from None
