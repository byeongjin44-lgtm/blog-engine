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


# ---------------------------------------------------------------------------------------------
# homerivet-json — mirrors src/lib/blog.ts + src/lib/content-policy.ts in the homerivettools repo.
# Keep in sync: the engine must reject everything the site's build would reject (an approved post
# that fails the site build never goes live). The engine adds stricter checks on top.
#
# LOCKED content rules (owner decision, 2026-11-01): material quantities only, numbers from the
# calculator code, cited sources, manufacturer-label reminder. Never loosen without approval.
# ---------------------------------------------------------------------------------------------

FORBIDDEN_TOPICS = {
    "electrical": ["wiring", "rewire", "circuit breaker", "electrical panel", "breaker box", "outlet installation",
                   "install an outlet", "light fixture installation"],
    "structural": ["load-bearing", "load bearing", "load calculation", "span table", "joist span", "joist sizing",
                   "beam size", "beam sizing", "footing size", "footing design", "post size", "structural design"],
    "gas": ["gas line", "gas pipe", "gas appliance installation"],
    "roofing": ["roofing", "roof repair", "shingle installation", "ladder safety"],
    "plumbing": ["plumbing installation", "pipe sweating", "drain line installation"],
    "hazardous": ["asbestos", "lead paint", "lead-based paint", "mold remediation", "mold removal"],
    "codes": ["building code", "code requirement", "permit requirement"],
}

_FORBIDDEN_PATTERNS = [
    (topic, phrase, re.compile(r"\b" + r"[\s-]+".join(map(re.escape, re.split(r"[ -]+", phrase))) + r"\b", re.I))
    for topic, phrases in FORBIDDEN_TOPICS.items() for phrase in phrases
]
MANUFACTURER_NOTE_RE = re.compile(r"manufacturer|product label|on the (?:bag|box|can|package|label)", re.I)
DECIMAL_RE = re.compile(r"(?<![\d.,])\d[\d,]*\.\d+(?![\d])")
EXTERNAL_LINK_RE = re.compile(r"\]\((?!/)[^)]*\)|https?://", re.I)


def find_forbidden(text: str) -> list[str]:
    return [f"{topic}: {phrase}" for topic, phrase, rx in _FORBIDDEN_PATTERNS if rx.search(text)]


HOMERIVET_SCHEMA = {
    "type": "object",
    "required": ["slug", "title", "description", "body"],
    "properties": {
        "slug": DEVRIVET_SCHEMA["properties"]["slug"],
        "title": DEVRIVET_SCHEMA["properties"]["title"],
        "description": DEVRIVET_SCHEMA["properties"]["description"],
        "body": {
            "type": "array",
            "description": "The post body as a list of blocks, in reading order.",
            "items": {"anyOf": DEVRIVET_SCHEMA["properties"]["body"]["items"]["anyOf"][:2]},
        },
    },
}


def homerivet_text(post: dict) -> str:
    """Reader-visible text, as the site's postText() builds it (title, description, all blocks)."""
    parts = [str(post.get("title", "")), str(post.get("description", ""))]
    for b in post.get("body") or []:
        if isinstance(b, dict):
            if "text" in b:
                parts.append(str(b["text"]))
            if isinstance(b.get("items"), list):
                parts += [str(x) for x in b["items"]]
    return "\n".join(parts)


def number_appears(number: str, text: str) -> bool:
    """Same rule as the site build: the exact number text, not inside a bigger number."""
    escaped = re.sub(r"([.,])", r"\\\1", number)
    return re.search(r"(^|[^\d.,])" + escaped + r"(?![\d]|[.,]\d)", text) is not None


def validate_homerivet(post: dict, site: Site, existing_slugs: set[str]) -> list[str]:
    from engine import calculator  # local import: formats must stay importable without the site repo

    errors = validate_devrivet(post, site, existing_slugs)
    if any(isinstance(b, dict) and b.get("type") == "code" for b in post.get("body") or []):
        errors.append("code blocks are not used on this site")
    text = homerivet_text(post)

    forbidden = find_forbidden(text)
    if forbidden:
        errors.append("forbidden topic (material quantities only): " + "; ".join(forbidden))
    if not MANUFACTURER_NOTE_RE.search(text):
        errors.append("must tell readers to check the manufacturer's label (e.g. 'check the coverage printed on the can')")
    if EXTERNAL_LINK_RE.search(_body_only(post)):
        errors.append("no external links in the body; sources are added automatically")

    sources = post.get("sources")
    if not isinstance(sources, list) or not sources:
        errors.append("sources must list at least one source")
    elif not all(isinstance(s, dict) and str(s.get("title", "")).strip() and re.match(r"^https://\S+$", str(s.get("url", "")))
                 for s in sources):
        errors.append("every source needs a title and an https url")

    calcs, results = post.get("calculations"), post.get("_calc_results")
    if not calcs or not results:
        errors.append("posts on this site need at least one calculator run (numbers must come from the calculator)")
        return errors
    for run_ in results:
        for r in run_["results"]:
            if r.get("primary") and not number_appears(r["number"], text):
                errors.append(f"calculator result \"{r['display']}\" ({r['label']}) must appear exactly as written")
    known = calculator.known_numbers(site, results, post.get("_calc_fields"))
    for m in DECIMAL_RE.findall(text):
        if float(m.replace(",", "")) not in known:
            errors.append(f"number {m} does not come from the calculator results — use only the numbers provided")
    return errors


def _body_only(post: dict) -> str:
    return homerivet_text({"body": post.get("body")})


def finalize_homerivet(post: dict, site: Site, topic, calc_results: list[dict] | None, calc_fields: list[dict] | None) -> None:
    """Engine-owned fields: the AI never writes sources or calculator inputs."""
    tool = site.tool(topic.related_tool)
    post["sources"] = [dict(s) for s in (tool.sources if tool else ())]
    post["calculations"] = [{"calculator": c["calculator"], "input": dict(c.get("input", {}))} for c in topic.calculations]
    post["_calc_results"] = calc_results or []
    used = {c["calculator"] for c in topic.calculations}
    post["_calc_fields"] = [f for f in (calc_fields or []) if f.get("calculator") in used]


def serialize_homerivet(post: dict) -> tuple[str, str]:
    keys = ("slug", "title", "description", "published", "updated", "relatedTool", "sources", "calculations", "body")
    ordered = {k: post[k] for k in keys if k in post}
    return f"{post['slug']}.json", json.dumps(ordered, ensure_ascii=False, indent=2) + "\n"


FORMATS["homerivet-json"] = {
    "schema": HOMERIVET_SCHEMA,
    "validate": validate_homerivet,
    "serialize": serialize_homerivet,
    "finalize": finalize_homerivet,
}
