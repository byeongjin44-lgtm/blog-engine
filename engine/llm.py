"""Claude API client. Structured output uses `output_config.format` (JSON schema outputs).

Docs: https://platform.claude.com/docs/en/build-with-claude/structured-outputs
"""

from __future__ import annotations

import copy
import json
import os
from typing import Any, Callable

from engine.http import request_json

API_URL = "https://api.anthropic.com/v1/messages"

class TruncatedError(RuntimeError):
    """The answer hit max_tokens before the JSON was complete."""


# Injected in tests: (payload) -> response dict.
Transport = Callable[[dict], dict]


def _default_transport(payload: dict) -> dict:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    return request_json(
        "POST",
        API_URL,
        payload,
        headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
        timeout=600,
    )


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


def structured_call(
    *,
    model: str,
    system: str,
    prompt: str,
    schema: dict,
    max_tokens: int = 8000,
    transport: Transport | None = None,
) -> dict[str, Any]:
    """Ask Claude for a JSON object matching `schema`; returns it parsed."""
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
        "output_config": {"format": {"type": "json_schema", "schema": strict_schema(schema)}},
    }
    response = (transport or _default_transport)(payload)
    stop = response.get("stop_reason")
    if stop == "max_tokens":
        raise TruncatedError(f"Claude response was cut off at {max_tokens} tokens")
    if stop == "refusal":
        raise RuntimeError("Claude declined to write this content")
    text = "".join(b.get("text", "") for b in response.get("content", []) if b.get("type") == "text")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Claude returned invalid JSON ({e}): {text[:300]}") from None
