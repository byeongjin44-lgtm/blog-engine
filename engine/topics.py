"""Topic queue: hand-written topics (sites/<id>/topics.toml) first, then topics the engine
proposes itself when the queue runs low (state/topics/<id>.json, committed by the workflows)."""

from __future__ import annotations

import json
import re
from pathlib import Path

from engine import state
from engine.config import Site, Topic
from engine.llm import Transport, structured_call

REFILL_BELOW = 7   # propose new topics when fewer than this many are left
REFILL_COUNT = 10  # how many to ask for


def _auto_path(site_id: str, state_dir: Path) -> Path:
    d = state_dir / "topics"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{site_id}.json"


def load_auto(site_id: str, state_dir: Path = state.STATE_DIR) -> list[dict]:
    path = _auto_path(site_id, state_dir)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def save_auto(site_id: str, topics: list[dict], state_dir: Path = state.STATE_DIR) -> None:
    _auto_path(site_id, state_dir).write_text(json.dumps(topics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def all_topics(site: Site, state_dir: Path = state.STATE_DIR) -> list[Topic]:
    auto = [Topic(title=t["title"], keyword=t["keyword"], related_tool=t.get("related_tool"), notes=t.get("notes", ""))
            for t in load_auto(site.id, state_dir)]
    return list(site.topics) + auto


def remaining(site: Site, state_dir: Path = state.STATE_DIR) -> list[Topic]:
    used = state.used_topics(site.id, state_dir)
    return [t for t in all_topics(site, state_dir) if t.title not in used]


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def propose(site: Site, known: list[str], transport: Transport | None = None, count: int = REFILL_COUNT) -> list[dict]:
    """Ask Claude for new search-driven topics that don't repeat `known` titles/keywords."""
    schema = {
        "type": "object",
        "required": ["topics"],
        "properties": {
            "topics": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["title", "keyword", "related_tool", "notes", "title_ko"],
                    "properties": {
                        "title": {"type": "string", "description": "Working title of the post"},
                        "keyword": {"type": "string", "description": "The exact phrase people type into Google"},
                        "related_tool": {"type": "string", "enum": [t.slug for t in site.tools]},
                        "notes": {"type": "string", "description": "What the post must cover and common mistakes to address"},
                        "title_ko": {"type": "string", "description": "Korean translation of the title"},
                    },
                },
            }
        },
    }
    tools = "\n".join(f"- {t.slug}: {t.name} — {t.description}" for t in site.tools)
    prompt = f"""Propose {count} new blog post topics for {site.name} ({site.url}), a site for {site.audience}.

Every topic must be tied to one of these tools (the post links to it):
{tools}

Choose questions real people search for often and that a short, accurate guide can answer better than
existing pages: specific how-tos, error messages, "X vs Y", "X for every <interval>", common patterns.
Avoid topics that need current news, version-specific claims or statistics. Spread topics across tools.

Do not repeat or closely paraphrase any of these existing topics:
{chr(10).join(f"- {k}" for k in known)}"""
    result = structured_call(
        model=site.reviewer_model,
        system="You are an SEO-minded technical editor planning a developer blog's content calendar.",
        prompt=prompt,
        schema=schema,
        max_tokens=4000,
        transport=transport,
    )
    seen = {_norm(k) for k in known}
    fresh: list[dict] = []
    for topic in result.get("topics", []):
        if site.tool(topic.get("related_tool")) is None:
            continue
        keys = {_norm(topic["title"]), _norm(topic["keyword"])}
        if keys & seen:
            continue
        seen |= keys
        fresh.append({**topic, "added": state.now_iso()})
    return fresh


def refill_if_low(site: Site, state_dir: Path = state.STATE_DIR, transport: Transport | None = None) -> list[dict]:
    """Returns the topics added (empty if the queue was long enough)."""
    if len(remaining(site, state_dir)) >= REFILL_BELOW:
        return []
    known_topics = all_topics(site, state_dir)
    known = [t.title for t in known_topics] + [t.keyword for t in known_topics]
    known += [d["post"]["title"] for d in state.all_drafts(state_dir) if d["site"] == site.id]
    added = propose(site, known, transport)
    if added:
        save_auto(site.id, load_auto(site.id, state_dir) + added, state_dir)
    return added
