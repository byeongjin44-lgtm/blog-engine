"""Tell search engines (Bing, Yandex, Naver, Seznam… via IndexNow) about newly published posts.

Best-effort only: a failure here never stops drafting or publishing and sends no Telegram message.
Runs from the hourly approval workflow. A post is announced once its page is actually live (the site
deploys a few minutes after the commit) and only while the key file is reachable on the site, so
nothing is sent before the site has the key. Posts that cannot be announced within GIVE_UP_DAYS are
left alone. Google does not use IndexNow; it finds posts through the sitemap.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from engine import state
from engine.config import load_site

ENDPOINT = "https://api.indexnow.org/indexnow"
GIVE_UP_DAYS = 3


def _http(method: str, url: str, body: dict | None = None) -> tuple[int, str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"User-Agent": "blog-engine"})
    if data is not None:
        req.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read(2000).decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read(2000).decode(errors="replace")


def run_notify(*, state_dir: Path = state.STATE_DIR, http=_http, now: datetime | None = None) -> int:
    """Announce published, not-yet-announced posts. Returns how many URLs were accepted."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=GIVE_UP_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    todo: dict[str, list[dict]] = {}
    for d in state.all_drafts(state_dir):
        if d["status"] == "published" and not d.get("indexnow") and d.get("decided", "") >= cutoff:
            todo.setdefault(d["site"], []).append(d)

    accepted = 0
    for site_id, drafts in todo.items():
        try:
            accepted += _notify_site(site_id, drafts, state_dir, http)
        except Exception as e:  # noqa: BLE001 — never let this break the workflow
            print(f"::warning title=IndexNow::[{site_id}] skipped: {e}")
    return accepted


def _notify_site(site_id: str, drafts: list[dict], state_dir: Path, http) -> int:
    site = load_site(site_id)
    if not site.indexnow_key:
        return 0
    key_url = f"{site.url}/{site.indexnow_key}.txt"
    status, body = http("GET", key_url)
    if status != 200 or body.strip() != site.indexnow_key:
        print(f"::notice title=IndexNow::[{site_id}] key file not live yet ({key_url}: {status}), will retry")
        return 0

    live = []
    for d in drafts:
        url = f"{site.url}/blog/{d['post']['slug']}"
        if http("GET", url)[0] == 200:
            live.append((d, url))
        else:
            print(f"IndexNow: {url} not live yet, will retry")
    if not live:
        return 0

    payload = {
        "host": urlparse(site.url).hostname,
        "key": site.indexnow_key,
        "keyLocation": key_url,
        "urlList": [url for _, url in live],
    }
    status, body = http("POST", ENDPOINT, payload)
    if status not in (200, 202):
        print(f"::warning title=IndexNow::[{site_id}] HTTP {status}: {body[:300]}")
        return 0
    for d, _ in live:
        d["indexnow"] = state.now_iso()
        state.save_draft(d, state_dir)
    print(f"IndexNow: announced {len(live)} URL(s) for {site_id}")
    return len(live)
