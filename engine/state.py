"""File-based state, committed back to this repo by the workflows (no database needed).

state/drafts/<id>.json  one file per draft, with a status: pending | published | rejected
state/telegram.json     the Telegram update offset already processed
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from engine.config import ROOT

STATE_DIR = ROOT / "state"


def _drafts_dir(state_dir: Path) -> Path:
    d = state_dir / "drafts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_draft_id(site_id: str, slug: str) -> str:
    return hashlib.sha1(f"{site_id}|{slug}|{now_iso()}".encode()).hexdigest()[:8]


def save_draft(draft: dict, state_dir: Path = STATE_DIR) -> Path:
    path = _drafts_dir(state_dir) / f"{draft['id']}.json"
    path.write_text(json.dumps(draft, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def load_draft(draft_id: str, state_dir: Path = STATE_DIR) -> dict | None:
    path = _drafts_dir(state_dir) / f"{draft_id}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def all_drafts(state_dir: Path = STATE_DIR) -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(_drafts_dir(state_dir).glob("*.json"))]


def used_slugs(site_id: str, state_dir: Path = STATE_DIR) -> set[str]:
    """Slugs already drafted for this site that are pending or published (rejected ones are free again)."""
    return {
        d["post"]["slug"]
        for d in all_drafts(state_dir)
        if d["site"] == site_id and d["status"] in ("pending", "published")
    }


def used_topics(site_id: str, state_dir: Path = STATE_DIR) -> set[str]:
    return {d["topic"]["title"] for d in all_drafts(state_dir) if d["site"] == site_id and d["status"] in ("pending", "published")}


def drafted_today(site_id: str, state_dir: Path = STATE_DIR) -> int:
    today = now_iso()[:10]
    return sum(1 for d in all_drafts(state_dir) if d["site"] == site_id and d["created"].startswith(today))


def get_offset(state_dir: Path = STATE_DIR) -> int:
    path = state_dir / "telegram.json"
    return json.loads(path.read_text())["offset"] if path.exists() else 0


def set_offset(offset: int, state_dir: Path = STATE_DIR) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "telegram.json").write_text(json.dumps({"offset": offset}) + "\n")


def _failed_path(site_id: str, state_dir: Path) -> Path:
    d = state_dir / "failed"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{site_id}.json"


def record_failed_topic(site_id: str, title: str, reason: str, state_dir: Path = STATE_DIR) -> None:
    path = _failed_path(site_id, state_dir)
    entries = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    entries.append({"title": title, "at": now_iso(), "reason": reason[:500]})
    path.write_text(json.dumps(entries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def recently_failed_topics(site_id: str, days: int = 14, state_dir: Path = STATE_DIR) -> set[str]:
    """Topics that failed recently are skipped for a while so they don't block every day."""
    path = _failed_path(site_id, state_dir)
    if not path.exists():
        return set()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {e["title"] for e in json.loads(path.read_text(encoding="utf-8")) if e["at"] >= cutoff}
