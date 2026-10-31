"""Minimal JSON-over-HTTPS helper (stdlib only, so the engine needs no pip install)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class HttpError(RuntimeError):
    def __init__(self, status: int, body: str, url: str):
        super().__init__(f"HTTP {status} from {url}: {body[:500]}")
        self.status = status
        self.body = body


def request_json(
    method: str,
    url: str,
    payload: Any | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 120,
) -> Any:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode()
    except urllib.error.HTTPError as e:
        raise HttpError(e.code, e.read().decode(errors="replace"), url) from None
    return json.loads(body) if body else None
