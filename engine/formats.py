"""Post formats. A format defines the JSON schema Claude fills, its validation, and the file it becomes.

`devrivet-json` mirrors `src/lib/blog.ts` in the DevRivet repo — keep the two in sync, or a
published post will fail the site's build-time validation.
"""

from __future__ import annotations

import json
import re

from engine.config import Site

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
TEXT_BLOCKS = {"p", "h2", "h3"}
LIST_BLOCKS = {"ul", "ol"}

DEFAULT_BANNED = (
    "in today's fast-paced",
    "in the ever-evolving",
    "delve into",
    "as an ai",
    "unlock the power",
    "game-changer",
    "look no further",
)

DEVRIVET_SCHEMA = {
    "type": "object",
    "required": ["slug", "title", "description", "body"],
    "properties": {
        "slug": {"type": "string", "description": "lowercase-kebab-case, 3-7 words, contains the main keyword"},
        "title": {"type": "string", "description": "Under 70 characters, contains the main keyword"},
        "description": {"type": "string", "description": "Meta description, 120-160 characters, states the answer"},
        "body": {
            "type": "array",
            "description": "The post body as a list of blocks, in reading order.",
            "items": {
                "anyOf": [
                    {
                        "type": "object",
                        "description": "Paragraph or heading. Inline `code` and [label](/path) links allowed.",
                        "required": ["type", "text"],
                        "properties": {
                            "type": {"type": "string", "enum": ["p", "h2", "h3"]},
                            "text": {"type": "string"},
                        },
                    },
                    {
                        "type": "object",
                        "description": "Bulleted (ul) or numbered (ol) list. Inline `code` and links allowed in items.",
                        "required": ["type", "items"],
                        "properties": {
                            "type": {"type": "string", "enum": ["ul", "ol"]},
                            "items": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                    {
                        "type": "object",
                        "description": "Code block. `text` holds the exact code, with \\n line breaks. Never empty.",
                        "required": ["type", "lang", "text"],
                        "properties": {
                            "type": {"type": "string", "enum": ["code"]},
                            "lang": {"type": "string", "description": "e.g. bash, javascript, python, json, text"},
                            "text": {"type": "string"},
                        },
                    },
                ]
            },
        },
    },
}


def _strip_markup(text: str) -> str:
    return LINK_RE.sub(r"\1", text).replace("`", "")


def body_text(body: list[dict]) -> str:
    parts: list[str] = []
    for block in body:
        if block.get("type") in LIST_BLOCKS:
            parts.extend(block.get("items", []))
        else:
            parts.append(block.get("text", ""))
    return "\n".join(_strip_markup(p) for p in parts)


def word_count(body: list[dict]) -> int:
    return len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'’-]*", body_text(body)))


def internal_links(body: list[dict]) -> list[str]:
    links: list[str] = []
    for block in body:
        texts = block.get("items", []) if block.get("type") in LIST_BLOCKS else [block.get("text", "")]
        for text in texts:
            if block.get("type") == "code":
                continue
            links += [href for _, href in LINK_RE.findall(text) if href.startswith("/")]
    return links


def validate_devrivet(post: dict, site: Site, existing_slugs: set[str]) -> list[str]:
    """Problems that block publishing (empty list = OK). Schema rules mirror DevRivet's validatePost."""
    errors: list[str] = []
    slug = post.get("slug", "")
    if not isinstance(slug, str) or not SLUG_RE.match(slug):
        errors.append("slug must be lowercase-kebab-case")
    elif slug in existing_slugs:
        errors.append(f"slug '{slug}' already exists")
    title = post.get("title", "")
    if not isinstance(title, str) or not title.strip():
        errors.append("title is required")
    elif len(title) > 75:
        errors.append(f"title is {len(title)} characters (max 75)")
    desc = post.get("description", "")
    if not isinstance(desc, str) or len(desc) < 50:
        errors.append("description must be at least 50 characters")
    elif len(desc) > 170:
        errors.append(f"description is {len(desc)} characters (max 170)")
    for key in ("published", "updated"):
        if key in post and not (isinstance(post[key], str) and DATE_RE.match(post[key])):
            errors.append(f"{key} must be YYYY-MM-DD")
    related = post.get("relatedTool")
    if related is not None and site.tool(related) is None:
        errors.append(f"relatedTool '{related}' is not a tool slug")

    body = post.get("body")
    if not isinstance(body, list) or not body:
        return errors + ["body must be a non-empty array"]
    for i, block in enumerate(body):
        kind = block.get("type") if isinstance(block, dict) else None
        if kind in TEXT_BLOCKS or kind == "code":
            if not isinstance(block.get("text"), str) or not block["text"].strip():
                errors.append(f"body[{i}] {kind} needs text")
        elif kind in LIST_BLOCKS:
            items = block.get("items")
            if not isinstance(items, list) or not items or not all(isinstance(x, str) and x.strip() for x in items):
                errors.append(f"body[{i}] {kind} needs non-empty items")
        else:
            errors.append(f"body[{i}] has unknown type '{kind}'")
    if errors:
        return errors

    # Quality checks beyond the site's schema.
    words = word_count(body)
    if words < site.min_words:
        errors.append(f"too short: {words} words (min {site.min_words})")
    if words > site.max_words:
        errors.append(f"too long: {words} words (max {site.max_words})")
    if not any(b["type"] == "h2" for b in body):
        errors.append("needs at least one h2 section")
    allowed_paths = {t.path for t in site.tools} | {f"/blog/{s}" for s in existing_slugs} | {"/blog"}
    for href in internal_links(body):
        if href not in allowed_paths:
            errors.append(f"internal link to unknown page {href}")
    tool = site.tool(related)
    if tool and tool.path not in internal_links(body):
        errors.append(f"must link to its tool at least once: [{tool.name}]({tool.path})")
    lowered = body_text(body).lower() + " " + str(title).lower()
    for phrase in DEFAULT_BANNED + site.banned_phrases:
        if phrase.lower() in lowered:
            errors.append(f"banned phrase: '{phrase}'")
    return errors


def serialize_devrivet(post: dict) -> tuple[str, str]:
    """(file name, file content) for the site repo."""
    ordered = {k: post[k] for k in ("slug", "title", "description", "published", "updated", "relatedTool", "body") if k in post}
    return f"{post['slug']}.json", json.dumps(ordered, ensure_ascii=False, indent=2) + "\n"


FORMATS = {
    "devrivet-json": {"schema": DEVRIVET_SCHEMA, "validate": validate_devrivet, "serialize": serialize_devrivet},
}


def get_format(name: str) -> dict:
    if name not in FORMATS:
        raise ValueError(f"unknown format '{name}' (known: {', '.join(FORMATS)})")
    return FORMATS[name]
