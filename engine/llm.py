"""Claude API client. Structured output is obtained by forcing a single tool call."""

from __future__ import annotations

import os
from typing import Any, Callable

from engine.http import request_json

API_URL = "https://api.anthropic.com/v1/messages"

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
        timeout=300,
    )


def structured_call(
    *,
    model: str,
    system: str,
    prompt: str,
    tool_name: str,
    tool_description: str,
    schema: dict,
    max_tokens: int = 8000,
    transport: Transport | None = None,
) -> dict[str, Any]:
    """Ask Claude to answer by calling `tool_name`; returns the tool input (already JSON)."""
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
        "tools": [{"name": tool_name, "description": tool_description, "input_schema": schema}],
        "tool_choice": {"type": "tool", "name": tool_name},
    }
    response = (transport or _default_transport)(payload)
    if response.get("stop_reason") == "max_tokens":
        raise RuntimeError("Claude response was cut off (max_tokens); increase max_tokens")
    for block in response.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == tool_name:
            return block["input"]
    raise RuntimeError(f"Claude did not call {tool_name}: {response}")
