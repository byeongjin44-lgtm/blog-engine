"""Classify failures: which ones the engine can recover from by itself, and which need the owner.

Owner-action errors come with Korean instructions, because the owner reads only Korean.
"""

from __future__ import annotations

import socket
import urllib.error

from engine.http import HttpError

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}


class OwnerActionRequired(RuntimeError):
    """Something only the owner can fix (keys, credit, tokens). Retrying will not help."""

    def __init__(self, message_ko: str, detail: str):
        super().__init__(detail)
        self.message_ko = message_ko


def is_transient(error: BaseException) -> bool:
    """Network hiccups and overloaded/rate-limited servers: worth retrying after a pause."""
    if isinstance(error, HttpError):
        return error.status in RETRYABLE_STATUS
    return isinstance(error, (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError))


def owner_action_for_anthropic(error: HttpError) -> OwnerActionRequired | None:
    body = error.body.lower()
    if error.status == 401:
        return OwnerActionRequired(
            "Claude API 키가 잘못되었거나 삭제되었습니다.\n"
            "① console.anthropic.com → API Keys에서 새 키 발급\n"
            "② GitHub blog-engine 저장소 → Settings → Secrets and variables → Actions → ANTHROPIC_API_KEY 값을 새 키로 교체",
            str(error),
        )
    if "credit balance" in body or "billing" in body or error.status == 402:
        return OwnerActionRequired(
            "Claude API 크레딧이 모두 소진되었습니다.\n"
            "console.anthropic.com → Settings → Billing에서 크레딧을 충전해 주세요. 충전 후에는 다음 날 자동으로 다시 작성합니다.",
            str(error),
        )
    if error.status == 403:
        return OwnerActionRequired(
            "Claude API 키에 권한이 없습니다. console.anthropic.com에서 키 상태와 사용 한도(spend limit)를 확인해 주세요.",
            str(error),
        )
    return None


def owner_action_for_github(error: HttpError) -> OwnerActionRequired | None:
    if error.status in (401, 403):
        return OwnerActionRequired(
            "사이트 저장소에 글을 올리는 GitHub 토큰이 만료되었거나 권한이 없습니다.\n"
            "① GitHub → Settings → Developer settings → Fine-grained tokens에서 새 토큰 발급 (사이트 저장소, Contents: Read and write)\n"
            "② blog-engine 저장소 → Settings → Secrets and variables → Actions → TARGET_GITHUB_TOKEN 값 교체",
            str(error),
        )
    return None
