"""Topic queue: hand-written topics (sites/<id>/topics.toml) first, then topics the engine
proposes itself when the queue runs low (state/topics/<id>.json, committed by the workflows)."""

from __future__ import annotations

import json
import re
from pathlib import Path

from engine import calculator, state
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
    auto = [Topic(title=t["title"], keyword=t["keyword"], related_tool=t.get("related_tool"), notes=t.get("notes", ""),
                  calculations=tuple(t.get("calculations", [])))
            for t in load_auto(site.id, state_dir)]
    return list(site.topics) + auto


def remaining(site: Site, state_dir: Path = state.STATE_DIR) -> list[Topic]:
    used = state.used_topics(site.id, state_dir) | state.recently_failed_topics(site.id, state_dir=state_dir)
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
    field_list: list[dict] = []
    calc_rules = ""
    if site.calculator:
        field_list = calculator.fields(site)
        item = schema["properties"]["topics"]["items"]
        item["required"].append("calculations")
        item["properties"]["calculations"] = {
            "type": "array",
            "description": "1-2 calculator runs giving the post's worked example (the numbers the post will use)",
            "items": {
                "type": "object",
                "required": ["calculator", "inputs"],
                "properties": {
                    "calculator": {"type": "string", "enum": [t.slug for t in site.tools]},
                    "inputs": {
                        "type": "array",
                        "description": "Only the inputs that differ from the defaults",
                        "items": {
                            "type": "object",
                            "required": ["key", "value"],
                            "properties": {"key": {"type": "string"}, "value": {"type": "string"}},
                        },
                    },
                },
            },
        }
        calc_rules = (
            "\n\nEvery topic is a material-quantity question with a concrete scenario (e.g. a 12x12 ft bedroom, "
            "a 10x10 ft patio slab 4 in thick). Give 1-2 calculator runs with realistic inputs for that scenario; "
            "the engine computes the numbers with the site's code. Only material quantities: never electrical, "
            "structural or load questions, gas, roofing, plumbing installation, hazardous materials or building codes.\n"
            "Calculator input fields (key, label, default, options):\n" + json.dumps(field_list, ensure_ascii=False)
        )
    prompt = f"""Propose {count} new blog post topics for {site.name} ({site.url}), a site for {site.audience}.

Every topic must be tied to one of these tools (the post links to it):
{tools}

Choose questions real people search for often and that a short, accurate guide can answer better than
existing pages: specific how-tos, error messages, "X vs Y", "X for every <interval>", common patterns.
Avoid topics that need current news, version-specific claims or statistics. Spread topics across tools.{calc_rules}

Do not repeat or closely paraphrase any of these existing topics:
{chr(10).join(f"- {k}" for k in known)}"""
    result = structured_call(
        model=site.reviewer_model,
        system=f"You are an SEO-minded editor planning the content calendar of {site.name}, a site about {site.niche}.",
        prompt=prompt,
        schema=schema,
        max_tokens=8000,  # thinking counts toward max_tokens (Claude 5.x); writing runs at low effort
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
        if site.calculator:
            topic = _with_valid_calculations(site, topic)
            if topic is None:
                continue
        seen |= keys
        fresh.append({**topic, "added": state.now_iso()})
    return fresh


def _parse_value(text: str):
    try:
        return float(text) if "." in text else int(text)
    except ValueError:
        return text


def _with_valid_calculations(site: Site, topic: dict) -> dict | None:
    """Keep a proposed topic only if it stays within the site's content rules and its calculator
    inputs run on the real calculator code."""
    from engine.formats import find_forbidden

    if find_forbidden(" ".join([topic["title"], topic["keyword"], topic.get("notes", "")])):
        return None
    calcs = [{"calculator": c["calculator"], "input": {i["key"]: _parse_value(i["value"]) for i in c.get("inputs", [])}}
             for c in topic.pop("calculations", [])]
    if not calcs or any(c["calculator"] != topic["related_tool"] for c in calcs):
        return None
    try:
        calculator.run(site, calcs)
    except calculator.CalculatorError:
        return None
    return {**topic, "calculations": calcs}


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
