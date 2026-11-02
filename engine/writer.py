"""Drafting: pick a topic, have Claude write the post, validate it, and review it in Korean."""

from __future__ import annotations

import json

from engine.config import Site, Topic
from engine.formats import get_format, word_count
from engine.llm import Transport, TruncatedError, structured_call

REVIEW_SCHEMA = {
    "type": "object",
    "required": ["title_ko", "summary_ko", "issues", "verdict"],
    "properties": {
        "title_ko": {"type": "string", "description": "Korean translation of the title"},
        "summary_ko": {
            "type": "array",
            "items": {"type": "string"},
            "description": "4-7 Korean bullet points covering what the post says, section by section (a back-translation check)",
        },
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["severity", "ko"],
                "properties": {
                    "severity": {"type": "string", "enum": ["high", "low"]},
                    "ko": {"type": "string", "description": "The problem, in Korean, quoting the English part it concerns"},
                    "en": {"type": "string", "description": "The same problem in English, for the writer"},
                },
            },
            "description": "high = factually wrong, code/command that would not work, misleading or unsafe advice; low = style/clarity",
        },
        "verdict": {"type": "string", "enum": ["ok", "fix"]},
    },
}


REVIEW_ROUNDS = 2  # rewrite rounds to resolve review comments


class DraftError(RuntimeError):
    pass


def _writer_system(site: Site) -> str:
    tools = "\n".join(f"- {t.name}: {t.path} — {t.description}" for t in site.tools)
    return f"""You write blog posts for {site.name} ({site.url}), a site for {site.audience}. Niche: {site.niche}.
Language: {site.language} (US English if "en").

Goal: the single most useful page on the web for the reader's search, so it earns search traffic honestly.

Rules:
- Answer the search question in the first 1-2 sentences, then explain. No intro fluff, no "In this article".
- Every code sample, command, expression and output must be correct and actually work. If unsure, leave it out.
- Prefer concrete examples, tables of common variants, and pitfalls readers actually hit.
- Never invent statistics, quotes, studies, version numbers or dates. No claims you cannot support.
- Plain, direct, friendly expert tone. Short paragraphs. No marketing language, no AI clichés.
- Internal links use [label](/path) and may ONLY point to these pages:
{tools}
  plus existing blog posts listed in the prompt. No other internal links. External links only to official documentation, and sparingly.
- End with a short FAQ section (h2 "FAQ", then h3 questions with p answers) answering 2-4 real follow-up questions.
- Use h2 for sections and h3 only under an h2. Do not include an h1 (the title is the h1).
{site.style_notes}""".strip()


def _writer_prompt(site: Site, topic: Topic, existing: dict[str, str], feedback: list[str] | None, previous: dict | None) -> str:
    tool = site.tool(topic.related_tool)
    existing_list = "\n".join(f"- /blog/{slug} — {title}" for slug, title in existing.items()) or "(none yet)"
    parts = [
        f"Write a post on: {topic.title}",
        f"Main search keyword: {topic.keyword}",
        f"Length: {site.min_words}-{site.max_words} words.",
    ]
    if tool:
        parts.append(f"The post is about our tool {tool.name} ({tool.path}). Link to it naturally at least once, early on.")
    if topic.notes:
        parts.append(f"Notes from the editor: {topic.notes}")
    parts.append(f"Existing blog posts you may link to:\n{existing_list}")
    if feedback and previous:
        parts.append(
            "Your previous draft had these problems. Fix all of them and return the full corrected post:\n"
            + "\n".join(f"- {f}" for f in feedback)
            + "\n\nPrevious draft:\n"
            + json.dumps(previous, ensure_ascii=False)
        )
    return "\n\n".join(parts)


def write_post(site: Site, topic: Topic, existing: dict[str, str], transport: Transport | None = None,
               feedback: list[str] | None = None, previous: dict | None = None) -> dict:
    fmt = get_format(site.format)
    post = structured_call(
        model=site.writer_model,
        system=_writer_system(site),
        prompt=_writer_prompt(site, topic, existing, feedback, previous),
        schema=fmt["schema"],
        max_tokens=16000,
        transport=transport,
    )
    if site.tool(topic.related_tool):
        post["relatedTool"] = topic.related_tool
    else:
        post.pop("relatedTool", None)
    return post


def review_post(site: Site, post: dict, transport: Transport | None = None) -> dict:
    system = (
        "You are a strict technical editor and a Korean translator. The site owner reads only Korean, "
        "so your Korean summary is how they decide whether to publish. Check every technical claim, "
        "code sample, command and expression for correctness. Be specific; do not invent problems."
    )
    prompt = f"Review this {site.language} blog post for {site.name} ({site.audience}).\n\n" + json.dumps(post, ensure_ascii=False)
    return structured_call(
        model=site.reviewer_model,
        system=system,
        prompt=prompt,
        schema=REVIEW_SCHEMA,
        max_tokens=4000,
        transport=transport,
    )


def draft(site: Site, topic: Topic, existing: dict[str, str], transport: Transport | None = None) -> tuple[dict, dict]:
    """Returns (post, review). Raises DraftError if no publishable draft could be produced."""
    fmt = get_format(site.format)
    existing_slugs = set(existing)

    try:
        post = write_post(site, topic, existing, transport)
    except TruncatedError:
        # Usually a post that tried to cover too much; one shorter attempt.
        shorter = (f"Your previous answer was too long and got cut off. Write a tighter post of at most "
                   f"{site.max_words} words: fewer, shorter code samples and only the most common cases.")
        post = write_post(site, topic, existing, transport, feedback=[shorter], previous={"note": "cut off"})
    errors = fmt["validate"](post, site, existing_slugs)
    if errors:
        post = write_post(site, topic, existing, transport, feedback=errors, previous=post)
        errors = fmt["validate"](post, site, existing_slugs)
        if errors:
            raise DraftError("draft failed validation twice: " + "; ".join(errors))

    # Apply every review comment (not only serious ones) before the owner sees the post: the owner
    # cannot act on English review notes, so they must be resolved here. Up to REVIEW_ROUNDS rounds.
    review = review_post(site, post, transport)
    applied: list[str] = []
    for _ in range(REVIEW_ROUNDS):
        issues = review.get("issues", [])
        if not issues:
            break
        revised = write_post(site, topic, existing, transport,
                             feedback=[i.get("en") or i["ko"] for i in issues], previous=post)
        if fmt["validate"](revised, site, existing_slugs):
            break  # the revision broke the post's structure: keep the last valid version
        post = revised
        applied += [i["ko"] for i in issues]
        review = review_post(site, post, transport)
    review["applied"] = applied
    review["word_count"] = word_count(post["body"])
    return post, review
