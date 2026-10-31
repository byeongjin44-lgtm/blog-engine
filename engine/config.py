"""Site configuration: one folder per site under sites/<id>/ (site.toml + topics.toml)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITES_DIR = ROOT / "sites"

DEFAULT_WRITER_MODEL = "claude-sonnet-5-5"
DEFAULT_REVIEWER_MODEL = "claude-haiku-5-5"


@dataclass(frozen=True)
class Tool:
    slug: str
    name: str
    path: str
    description: str


@dataclass(frozen=True)
class Topic:
    title: str
    keyword: str
    related_tool: str | None = None
    notes: str = ""


@dataclass(frozen=True)
class Site:
    id: str
    name: str
    url: str
    language: str
    audience: str
    niche: str
    format: str
    publisher: dict
    posts_per_day: int = 1
    min_words: int = 700
    max_words: int = 1800
    tools: tuple[Tool, ...] = ()
    topics: tuple[Topic, ...] = ()
    writer_model: str = DEFAULT_WRITER_MODEL
    reviewer_model: str = DEFAULT_REVIEWER_MODEL
    style_notes: str = ""
    banned_phrases: tuple[str, ...] = field(default_factory=tuple)

    def tool(self, slug: str | None) -> Tool | None:
        return next((t for t in self.tools if t.slug == slug), None)


def load_site(site_id: str, sites_dir: Path = SITES_DIR) -> Site:
    folder = sites_dir / site_id
    with open(folder / "site.toml", "rb") as f:
        raw = tomllib.load(f)
    topics_raw: dict = {}
    if (folder / "topics.toml").exists():
        with open(folder / "topics.toml", "rb") as f:
            topics_raw = tomllib.load(f)

    if raw.get("id") != site_id:
        raise ValueError(f"sites/{site_id}/site.toml must have id = \"{site_id}\"")
    tools = tuple(Tool(**t) for t in raw.get("tools", []))
    tool_slugs = {t.slug for t in tools}
    topics = tuple(Topic(**t) for t in topics_raw.get("topics", []))
    for t in topics:
        if t.related_tool and t.related_tool not in tool_slugs:
            raise ValueError(f"topic {t.title!r} references unknown tool {t.related_tool!r}")
    models = raw.get("models", {})
    return Site(
        id=raw["id"],
        name=raw["name"],
        url=raw["url"].rstrip("/"),
        language=raw.get("language", "en"),
        audience=raw["audience"],
        niche=raw["niche"],
        format=raw["format"],
        publisher=raw["publisher"],
        posts_per_day=int(raw.get("posts_per_day", 1)),
        min_words=int(raw.get("min_words", 700)),
        max_words=int(raw.get("max_words", 1800)),
        tools=tools,
        topics=topics,
        writer_model=models.get("writer", DEFAULT_WRITER_MODEL),
        reviewer_model=models.get("reviewer", DEFAULT_REVIEWER_MODEL),
        style_notes=raw.get("style_notes", ""),
        banned_phrases=tuple(raw.get("banned_phrases", [])),
    )


def all_site_ids(sites_dir: Path = SITES_DIR) -> list[str]:
    return sorted(p.name for p in sites_dir.iterdir() if (p / "site.toml").exists())
