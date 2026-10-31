"""Publishers deliver an approved post to a site.

github_files: commits the post file into the site's GitHub repo (the site's own build/deploy,
e.g. Cloudflare Pages, takes it from there). A WordPress publisher is planned for later sites.
"""

from __future__ import annotations

import base64
import os
from typing import Callable
from urllib.parse import quote

from engine.http import HttpError, request_json

GITHUB_API = "https://api.github.com"

# (method, url, payload) -> response; injected in tests.
GitHubTransport = Callable[[str, str, dict | None], object]


class GitHubFilesPublisher:
    def __init__(self, config: dict, transport: GitHubTransport | None = None):
        self.repo = config["repo"]
        self.branch = config.get("branch", "main")
        self.path = config["path"].strip("/")
        self.token_env = config.get("token_env", "TARGET_GITHUB_TOKEN")
        self._transport = transport

    def _call(self, method: str, url: str, payload: dict | None = None):
        if self._transport:
            return self._transport(method, url, payload)
        token = os.environ.get(self.token_env)
        if not token:
            raise RuntimeError(f"{self.token_env} is not set")
        return request_json(
            method,
            url,
            payload,
            headers={"Authorization": f"Bearer {token}", "X-GitHub-Api-Version": "2022-11-28"},
        )

    def existing_slugs(self) -> set[str]:
        url = f"{GITHUB_API}/repos/{self.repo}/contents/{quote(self.path)}?ref={quote(self.branch)}"
        try:
            entries = self._call("GET", url)
        except HttpError as e:
            if e.status == 404:
                return set()
            raise
        return {e["name"].rsplit(".", 1)[0] for e in entries if e.get("type") == "file"}

    def publish(self, filename: str, content: str, message: str) -> str:
        file_path = f"{self.path}/{filename}"
        url = f"{GITHUB_API}/repos/{self.repo}/contents/{quote(file_path)}"
        payload = {
            "message": message,
            "content": base64.b64encode(content.encode()).decode(),
            "branch": self.branch,
        }
        result = self._call("PUT", url, payload)
        return result.get("content", {}).get("html_url", file_path) if isinstance(result, dict) else file_path


PUBLISHERS = {"github_files": GitHubFilesPublisher}


def get_publisher(config: dict, transport=None):
    kind = config.get("type")
    if kind not in PUBLISHERS:
        raise ValueError(f"unknown publisher type '{kind}' (known: {', '.join(PUBLISHERS)})")
    return PUBLISHERS[kind](config, transport=transport)
