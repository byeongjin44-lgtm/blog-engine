"""Per-post spending cap (owner decision, 2026-11-02).

The owner allows, per post, at most the path "Haiku fails -> Sonnet succeeds + 2 review rewrite rounds"
(about 150 KRW). Past that, the post is abandoned for the day. These values are locked by tests:
never change them without the owner's explicit approval.

CostGuard wraps the Claude API transport. Before each call it lowers `max_tokens` so the call cannot
push the post over budget; after each call it adds the real cost from the response's `usage`.
"""

from __future__ import annotations

import json
from typing import Callable

BUDGET_KRW = 150           # per post, hard limit
KRW_PER_USD = 1450         # conservative rate (higher rate = smaller dollar budget)
MIN_OUTPUT_TOKENS = 1500   # a call that cannot afford this much output is not worth making

# USD per million tokens (input, output). Unknown models are priced conservatively high.
PRICES = {"haiku": (0.10, 0.50), "sonnet": (2.00, 10.00)}
UNKNOWN_PRICE = (5.00, 25.00)


class BudgetExceeded(RuntimeError):
    """The post's spending cap is reached; stop working on it."""


def price(model: str) -> tuple[float, float]:
    return next((p for key, p in PRICES.items() if key in model), UNKNOWN_PRICE)


def budget_usd() -> float:
    return BUDGET_KRW / KRW_PER_USD


class CostGuard:
    def __init__(self, transport: Callable[[dict], dict], limit_usd: float | None = None):
        self.inner = transport
        self.limit = budget_usd() if limit_usd is None else limit_usd
        self.spent = 0.0
        self.calls = 0

    @property
    def spent_krw(self) -> int:
        return round(self.spent * KRW_PER_USD)

    def __call__(self, payload: dict) -> dict:
        p_in, p_out = price(payload["model"])
        # ~3 characters per token is a deliberately high estimate of the input size.
        est_in = len(json.dumps(payload, ensure_ascii=False)) / 3
        remaining = self.limit - self.spent - est_in * p_in / 1e6
        affordable = int(remaining * 1e6 / p_out) if remaining > 0 else 0
        if affordable < MIN_OUTPUT_TOKENS:
            raise BudgetExceeded(f"글 1개당 상한 {BUDGET_KRW}원 도달 (사용 {self.spent_krw}원)")
        payload = {**payload, "max_tokens": min(payload["max_tokens"], affordable)}
        response = self.inner(payload)
        self.calls += 1
        usage = response.get("usage") or {}
        out_text = "".join(b.get("text", "") for b in response.get("content", []) if isinstance(b, dict))
        in_tokens = usage.get("input_tokens", est_in)
        out_tokens = usage.get("output_tokens", len(out_text) / 3)
        self.spent += in_tokens * p_in / 1e6 + out_tokens * p_out / 1e6
        return response
