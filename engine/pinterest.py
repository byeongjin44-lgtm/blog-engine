"""Pinterest pin drafts sent to Telegram for the owner to post by hand (no Pinterest API, no AI cost).

For sites with `[pinterest] enabled = true`, once a published post's page and its tall image
(<url>/pin/blog-<slug>.png, built by the site) are live, this sends two Telegram messages marked
"[핀터레스트]": the image, then the pin title, description and link to copy, with the Korean meaning
and the suggested board. Each post is sent once. Text is built from the post itself and the
calculator results the engine computed, so every number matches the post.

Best-effort only, like IndexNow: a failure is retried on the next hourly run, never stops drafting,
approval or publishing, and posts not sent within GIVE_UP_DAYS are left alone. It has no buttons,
so it cannot be mistaken for a post approval message.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from engine import state
from engine.config import Site, load_site
from engine.indexnow import _http
from engine.telegram import Telegram, esc

GIVE_UP_DAYS = 7
MAX_TITLE = 100        # Pinterest pin title limit
MAX_DESCRIPTION = 500  # Pinterest pin description limit
TAG = "📌 [핀터레스트]"


def _differing_inputs(calcs: list[dict]) -> set[str]:
    keys = {k for c in calcs for k in c.get("input", {})}
    return {k for k in keys if len({str(c.get("input", {}).get(k, "")) for c in calcs}) > 1}


def answer_lines(post: dict) -> list[str]:
    """Headline answers, e.g. "56 bags (Bag size: 80 lb)" — the same rule as the site's pin image."""
    calcs = post.get("_calc_results") or []
    # _calc_fields has one entry per calculator (not per calculation), so look fields up by name.
    fields_by_name = {f.get("calculator"): f.get("fields", []) for f in post.get("_calc_fields") or []}
    differing = _differing_inputs(calcs) if len(calcs) > 1 else set()
    lines = []
    for c in calcs:
        answer = " · ".join(r["display"] for r in c.get("results", []) if r.get("primary"))
        if not answer:
            continue
        fields = fields_by_name.get(c.get("calculator"), [])
        parts = []
        for key in c.get("input", {}):
            if key not in differing:
                continue
            field = next((f for f in fields if f.get("key") == key), None)
            value = str(c["input"][key])
            if field and field.get("kind") == "select":
                label = next((o["label"] for o in field.get("options", []) if o.get("value") == value), value)
                short = re.sub(r"\s*\(.*\)\s*$", "", label)
                parts.append(f"{field['label']}: {short}")
            elif field:
                parts.append(f"{field['label']}: {value}{' ' + field['unit'] if field.get('unit') else ''}")
        lines.append(f"{answer} ({', '.join(parts)})" if parts else answer)
    return lines[:3]


def pin_text(site: Site, post: dict) -> dict:
    """Title, description, link and board for one pin."""
    answers = answer_lines(post)
    tool = site.tool(post.get("relatedTool"))
    description = post.get("description", "").strip()
    if answers:
        description += f" Quick answer: {'; '.join(answers)}."
    if tool:
        description += f" Use the free {tool.name} on {site.name} to enter your own measurements."
    description += " Always check the coverage printed on the product label."
    boards = site.pinterest.get("boards", {}) if site.pinterest else {}
    board = boards.get(post.get("relatedTool") or "", site.pinterest.get("default_board", site.name) if site.pinterest else site.name)
    return {
        "title": post.get("title", "")[:MAX_TITLE],
        "description": description[:MAX_DESCRIPTION],
        "link": f"{site.url}/blog/{post['slug']}",
        "image": f"{site.url}/pin/blog-{post['slug']}.png",
        "board": board,
        "answers": answers,
    }


def _message(site: Site, draft: dict, pin: dict) -> str:
    review = draft.get("review") or {}
    board_ko = (site.pinterest or {}).get("boards_ko", {}).get(pin["board"], "")
    lines = [
        f"<b>{TAG} {esc(site.name)}</b> — 위 이미지로 핀 1개 올리기",
        "",
        f"글 뜻: {esc(review.get('title_ko', ''))}",
    ]
    if pin["answers"]:
        lines.append(f"계산 결과: {esc(' / '.join(pin['answers']))}")
    lines += [
        "",
        "<b>① 핀 제목</b> (길게 눌러 복사)",
        f"<code>{esc(pin['title'])}</code>",
        "",
        "<b>② 설명</b> (글 요약 + 계산 결과 + 무료 계산기 안내 + 제품 표기 확인 안내)",
        f"<code>{esc(pin['description'])}</code>",
        "",
        "<b>③ 링크</b>",
        f"<code>{esc(pin['link'])}</code>",
        "",
        f"<b>④ 보드</b>: {esc(pin['board'])}" + (f" ({esc(board_ko)})" if board_ko else ""),
        "",
        "올리는 법: 위 이미지 저장 → 핀터레스트 앱 ＋ → 핀 → 이미지 선택 → ①②③ 붙여넣기 → ④ 보드 선택 → 게시",
    ]
    return "\n".join(lines)


def run_pins(*, state_dir: Path = state.STATE_DIR, http=_http, telegram: Telegram | None = None,
             now: datetime | None = None, sites_dir: Path | None = None) -> int:
    """Send pin drafts for published, live, not-yet-sent posts. Returns how many were sent."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=GIVE_UP_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    telegram = telegram or Telegram()
    sites: dict[str, Site | None] = {}
    sent = 0
    for d in sorted(state.all_drafts(state_dir), key=lambda d: d.get("decided", "")):
        if d.get("status") != "published" or d.get("pin_sent") or d.get("decided", "") < cutoff:
            continue
        try:
            if d["site"] not in sites:
                site = load_site(d["site"], sites_dir) if sites_dir else load_site(d["site"])
                sites[d["site"]] = site if site.pinterest and site.pinterest.get("enabled") else None
            site = sites[d["site"]]
            if not site:
                continue
            pin = pin_text(site, d["post"])
            if http("GET", pin["link"])[0] != 200 or http("GET", pin["image"])[0] != 200:
                print(f"Pinterest: {pin['link']} or its image not live yet, will retry")
                continue
            telegram.call("sendPhoto", {"chat_id": telegram.chat_id, "photo": pin["image"],
                                        "caption": f"{TAG} {site.name}: {pin['title']}"[:1000]})
            telegram.send(_message(site, d, pin))
            d["pin_sent"] = state.now_iso()
            state.save_draft(d, state_dir)
            sent += 1
        except Exception as e:  # noqa: BLE001 — never let this break the workflow
            print(f"::warning title=Pinterest::[{d.get('site')}] {d.get('id')}: {e}")
    return sent
