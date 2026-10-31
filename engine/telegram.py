"""Telegram Bot API: draft notifications with Approve / Reject buttons, polled via getUpdates."""

from __future__ import annotations

import html
import os
from typing import Callable

from engine.http import request_json

# (method, payload) -> result; injected in tests.
TelegramTransport = Callable[[str, dict], object]

MAX_MESSAGE = 4000  # Telegram's limit is 4096 characters


class Telegram:
    def __init__(self, transport: TelegramTransport | None = None):
        self._transport = transport
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    def call(self, method: str, payload: dict):
        if self._transport:
            return self._transport(method, payload)
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token or not self.chat_id:
            raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set")
        response = request_json("POST", f"https://api.telegram.org/bot{token}/{method}", payload, timeout=60)
        if not response.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {response}")
        return response["result"]

    def send(self, text: str, buttons: list[list[dict]] | None = None) -> int:
        payload = {
            "chat_id": self.chat_id,
            "text": text[:MAX_MESSAGE],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        result = self.call("sendMessage", payload)
        return result["message_id"] if isinstance(result, dict) else 0

    def edit(self, message_id: int, text: str) -> None:
        self.call(
            "editMessageText",
            {"chat_id": self.chat_id, "message_id": message_id, "text": text[:MAX_MESSAGE], "parse_mode": "HTML", "disable_web_page_preview": True},
        )

    def answer(self, callback_id: str, text: str) -> None:
        self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    def updates(self, offset: int) -> list[dict]:
        return self.call("getUpdates", {"offset": offset, "timeout": 0, "allowed_updates": ["callback_query"]}) or []


def esc(text: str) -> str:
    return html.escape(str(text), quote=False)


def approval_buttons(draft_id: str) -> list[list[dict]]:
    return [[
        {"text": "✅ 승인 (발행)", "callback_data": f"approve:{draft_id}"},
        {"text": "❌ 폐기", "callback_data": f"reject:{draft_id}"},
    ]]
