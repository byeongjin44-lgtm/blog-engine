"""Per-post spending cap (owner decision, 2026-11-02).

The owner allows, per post, at most the path "Haiku fails -> Sonnet succeeds + 2 review rewrite rounds"
(about 150 KRW). Past that, the post is abandoned for the day. These values are locked by tests:
never change them without the owner's explicit approval.

Finishing grace (owner decision, 2026-11-05): an attempt that is already running may go slightly over
150 KRW — up to HARD_LIMIT_KRW — so a nearly finished post is completed and sent instead of failing.
New work (a fallback model attempt, review rewrite rounds) still only starts within BUDGET_KRW.

CostGuard wraps the Claude API transport. Before each call it lowers `max_tokens` so the call cannot
push the post over the limit; after each call it adds the real cost from the response's `usage`.
Calls are held to BUDGET_KRW, except that a call which could not afford FINISH_TOKENS of output within it
may use up to that much within HARD_LIMIT_KRW. Calls inside `guard.optional_work()` get no grace.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Callable

BUDGET_KRW = 150           # per post: no new work starts past this
FINISH_GRACE_KRW = 50      # a running attempt may finish up to this much over (owner decision, 2026-11-05)
HARD_LIMIT_KRW = BUDGET_KRW + FINISH_GRACE_KRW  # never exceeded
KRW_PER_USD = 1450         # conservative rate (higher rate = smaller dollar budget)
MIN_OUTPUT_TOKENS = 1500   # a call that cannot afford this much output is not worth making
REVIEW_RESERVE_KRW = 20    # part of the grace kept for the Korean review, so a finished post can still be sent
FINISH_TOKENS = 4000       # output a finishing call may always get (within HARD_LIMIT_KRW): about one full post

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
        self.hard_limit = self.limit * HARD_LIMIT_KRW / BUDGET_KRW
        self.spent = 0.0
        self.calls = 0
        self._optional = 0
        self._review = 0

    @property
    def over_budget(self) -> bool:
        """True once the post has used its normal budget: start no new work."""
        return self.spent >= self.limit

    @contextmanager
    def optional_work(self):
        """Extra work (review rewrite rounds) that must stay within the normal budget."""
        self._optional += 1
        try:
            yield
        finally:
            self._optional -= 1

    @property
    def spent_krw(self) -> int:
        return round(self.spent * KRW_PER_USD)

    @contextmanager
    def final_review(self):
        """The Korean review of a finished post: it may use the reserve kept inside the grace."""
        self._review += 1
        try:
            yield
        finally:
            self._review -= 1

    def __call__(self, payload: dict) -> dict:
        p_in, p_out = price(payload["model"])
        # ~3 characters per token is a deliberately high estimate of the input size.
        est_in = len(json.dumps(payload, ensure_ascii=False)) / 3
        est_in_usd = est_in * p_in / 1e6

        def tokens_within(limit: float) -> int:
            remaining = limit - self.spent - est_in_usd
            return int(remaining * 1e6 / p_out) if remaining > 0 else 0

        affordable = tokens_within(self.limit)
        if not self._optional:
            # Finishing grace: if the normal budget cannot pay for a complete answer, allow up to
            # FINISH_TOKENS more output within the hard limit, so the attempt can be finished.
            reserve = 0 if self._review else self.limit * REVIEW_RESERVE_KRW / BUDGET_KRW
            affordable = max(affordable, min(tokens_within(self.hard_limit - reserve), FINISH_TOKENS))
        if affordable < MIN_OUTPUT_TOKENS:
            cap = BUDGET_KRW if self._optional else HARD_LIMIT_KRW
            raise BudgetExceeded(f"글 1개당 상한 {cap}원 도달 (사용 {self.spent_krw}원)")
        payload = {**payload, "max_tokens": min(payload["max_tokens"], affordable)}
        response = dict(self.inner(payload))
        response["_max_tokens"] = payload["max_tokens"]  # the real cap, for error messages
        self.calls += 1
        usage = response.get("usage") or {}
        out_text = "".join(b.get("text", "") for b in response.get("content", []) if isinstance(b, dict))
        in_tokens = usage.get("input_tokens", est_in)
        out_tokens = usage.get("output_tokens", len(out_text) / 3)
        self.spent += in_tokens * p_in / 1e6 + out_tokens * p_out / 1e6
        return response
