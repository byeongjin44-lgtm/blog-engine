"""Entry points used by the GitHub Actions workflows.

    python -m engine draft            # draft today's posts for every site (respects posts_per_day)
    python -m engine draft --site X   # one site only
    python -m engine poll             # process Telegram Approve / Reject buttons once
    python -m engine listen           # wait for buttons and handle them immediately (up to 4 h)
    python -m engine check            # validate every site config (used by the test workflow)
    python -m engine notify           # announce newly published posts via IndexNow (best-effort)
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from engine import calculator, state
from engine.config import Site, all_site_ids, load_site
from engine.budget import BUDGET_KRW, HARD_LIMIT_KRW, BudgetExceeded, CostGuard
from engine.errors import OwnerActionRequired
from engine.llm import _default_transport as default_transport
from engine.formats import get_format
from engine.publishers import get_publisher
from engine.telegram import Telegram, approval_buttons, esc
from engine import topics as topic_queue
from engine.writer import DraftError, draft


def draft_link(draft_id: str) -> str:
    repo = os.environ.get("GITHUB_REPOSITORY", "byeongjin44-lgtm/blog-engine")
    return f"https://github.com/{repo}/blob/main/state/drafts/{draft_id}.json"


def format_review_message(site: Site, d: dict) -> str:
    post, review = d["post"], d["review"]
    lines = [
        f"📝 <b>[{esc(site.name)}] 새 글 검토</b>",
        "",
        f"<b>제목</b>: {esc(post['title'])}",
        f"<b>한글 제목</b>: {esc(review.get('title_ko', ''))}",
        f"<b>키워드</b>: {esc(d['topic']['keyword'])}",
        f"<b>분량</b>: {review.get('word_count', '?')}단어",
        f"<b>작성 비용</b>: 약 {d.get('cost_krw', '?')}원 (모델: {esc(d.get('model', '?'))})"
        + (f" — 마지막 시도를 끝까지 마치느라 한도 {BUDGET_KRW}원을 조금 넘었습니다 (최대 {HARD_LIMIT_KRW}원)"
           if isinstance(d.get("cost_krw"), int) and d["cost_krw"] > BUDGET_KRW else ""),
        "",
    ]
    calc_results = post.get("_calc_results") or []
    if calc_results:
        lines.append("<b>글에 쓰인 계산기 결과</b> (코드로 계산, 본문 숫자 자동 대조 통과)")
        for run_ in calc_results:
            headline = ", ".join(r["display"] for r in run_["results"] if r.get("primary"))
            lines.append(f"🧮 {esc(run_['calculator'])}: {esc(headline)}")
        lines.append("")
    lines += [
        "<b>내용 요약</b>",
        *[f"• {esc(s)}" for s in review.get("summary_ko", [])],
        "",
    ]
    applied = review.get("applied", [])
    if applied:
        lines.append(f"<b>검토 의견 {len(applied)}건 자동 반영 완료</b>")
        lines += [f"✔️ {esc(a)}" for a in applied]
        lines.append("")
    issues = review.get("issues", [])
    if issues:
        lines.append("<b>자동 반영 후에도 남은 의견</b> (내용을 보고 승인 또는 폐기만 결정하시면 됩니다)")
        for issue in issues:
            mark = "⚠️" if issue.get("severity") == "high" else "ℹ️"
            lines.append(f"{mark} {esc(issue.get('ko', ''))}")
    else:
        lines.append("<b>최종 검토</b>: 남은 문제 없음 ✅")
    lines += ["", f'<a href="{draft_link(d["id"])}">전체 원고 보기</a>']
    return "\n".join(lines)


def existing_posts(site: Site, publisher, state_dir: Path) -> dict[str, str]:
    """slug -> title of posts already on the site or waiting for approval."""
    titles = {d["post"]["slug"]: d["post"]["title"] for d in state.all_drafts(state_dir)
              if d["site"] == site.id and d["status"] in ("pending", "published")}
    for slug in publisher.existing_slugs():
        titles.setdefault(slug, slug.replace("-", " "))
    return titles


def run_draft(site_ids: list[str], *, state_dir: Path = state.STATE_DIR, telegram: Telegram | None = None,
              llm_transport=None, github_transport=None, extra: int = 0) -> int:
    tg = telegram or Telegram()
    failures = 0
    for site_id in site_ids:
        try:
            failures += _draft_site(site_id, tg, state_dir, llm_transport, github_transport, extra=extra)
        except Exception as e:  # noqa: BLE001 — setup errors (bad token, config) must still be reported
            failures += 1
            report_error(tg, f"[{site_id}] 초안 작업 오류", e)
    return failures


def report_error(tg: Telegram, title: str, error: Exception) -> None:
    """Show the error as a GitHub Actions annotation (visible on the run page) and in Telegram."""
    message = f"{type(error).__name__}: {error}"
    print(f"::error title={title}::{message[:1500]}".replace("\n", " "))
    try:
        tg.send(f"🚫 <b>{esc(title)}</b>\n{esc(message)[:1500]}")
    except Exception as tg_error:  # noqa: BLE001
        print(f"::error title=Telegram 전송 실패::{type(tg_error).__name__}: {tg_error}"[:1500].replace("\n", " "))


# Owner decision (2026-11-02, cost): one topic per post per day, at most Haiku then Sonnet, within the
# per-post budget in engine/budget.py. If that fails, stop for the day. Locked by tests.
MAX_TOPICS_PER_POST = 1


def _draft_site(site_id: str, tg: Telegram, state_dir: Path, llm_transport, github_transport, extra: int = 0) -> int:
    failures = 0
    site = load_site(site_id)
    publisher = get_publisher(site.publisher, transport=github_transport)
    wanted = max(0, site.posts_per_day - state.drafted_today(site.id, state_dir)) + extra
    for _ in range(wanted):
        try:
            if not _draft_one(site, tg, state_dir, publisher, llm_transport):
                failures += 1
                break  # do not spend more today: the next scheduled run tries again
        except OwnerActionRequired as e:
            failures += 1
            print(f"::error title=사용자 조치 필요::{e}".replace("\n", " ")[:1500])
            tg.send(f"🛠 <b>[{esc(site.name)}] 직접 처리해 주셔야 할 일이 있습니다</b>\n{esc(e.message_ko)}")
            break  # retrying cannot help until the owner fixes it
    return failures


def _draft_one(site: Site, tg: Telegram, state_dir: Path, publisher, llm_transport) -> bool:
    """Draft one post, recovering on its own: retry with the fallback model, then try the next topic.
    Returns False only if every attempt failed (the owner then gets one summary message)."""
    try:
        added = topic_queue.refill_if_low(site, state_dir, transport=llm_transport)
    except OwnerActionRequired:
        raise
    except Exception as e:  # noqa: BLE001 — drafting can continue with the topics left
        added = []
        print(f"::warning title=주제 자동 보충 실패::{e}"[:1500].replace("\n", " "))
    if added:
        lines = "\n".join(f"• {esc(t['title_ko'])} <i>({esc(t['keyword'])})</i>" for t in added)
        tg.send(f"🗂 <b>[{esc(site.name)}]</b> 주제가 부족해 새 주제 {len(added)}개를 추가했습니다.\n{lines}")

    candidates = topic_queue.remaining(site, state_dir)[:MAX_TOPICS_PER_POST]
    if not candidates:
        tg.send(f"📭 <b>[{esc(site.name)}]</b> 쓸 수 있는 주제가 없습니다. sites/{site.id}/topics.toml에 주제를 추가해 주세요.")
        return False

    models = list(dict.fromkeys([site.writer_model, site.fallback_model]))[:2]
    attempts: list[str] = []
    guard = CostGuard(llm_transport or default_transport)  # one budget for this whole post
    for topic in candidates:
        existing = existing_posts(site, publisher, state_dir)
        calc = None
        if site.calculator:
            # Numbers come from the site's calculator code, computed before any AI call (no cost).
            try:
                if not topic.calculations:
                    raise calculator.CalculatorError("topic has no calculations")
                calc = {"results": calculator.run(site, list(topic.calculations)), "fields": calculator.fields(site)}
            except calculator.CalculatorError as e:
                attempts.append(f"{topic.title} / 계산기: {e}")
                print(f"::warning title=계산기 오류::{topic.title}: {e}"[:1500].replace("\n", " "))
                state.record_failed_topic(site.id, topic.title, attempts[-1], state_dir)
                continue
        for model in models:
            if guard.over_budget:
                # A new attempt only starts within the normal budget; a running one may finish slightly over.
                attempts.append(f"{topic.title} / {model}: 글 1개당 한도 {BUDGET_KRW}원을 이미 써서 시도하지 않음")
                break
            try:
                post, review = draft(replace(site, writer_model=model), topic, existing, transport=guard, calc=calc)
            except OwnerActionRequired:
                raise
            except BudgetExceeded as e:
                attempts.append(f"{topic.title} / {model}: {e}")
                break
            except Exception as e:  # noqa: BLE001 — recover by changing model, then topic
                reason = f"{type(e).__name__}: {e}"
                attempts.append(f"{topic.title} / {model}: {reason[:200]}")
                print(f"::warning title=자동 복구 시도::{topic.title} / {model}: {reason}"[:1500].replace("\n", " "))
                continue
            d = {
                "id": state.new_draft_id(site.id, post["slug"]),
                "site": site.id,
                "status": "pending",
                "created": state.now_iso(),
                "topic": {"title": topic.title, "keyword": topic.keyword, "related_tool": topic.related_tool,
                          **({"calculations": list(topic.calculations)} if topic.calculations else {})},
                "post": post,
                "review": review,
                "model": model,
                "recovered_from": attempts,
                "cost_krw": guard.spent_krw,
                "api_calls": guard.calls,
            }
            state.save_draft(d, state_dir)
            d["telegram_message_id"] = tg.send(format_review_message(site, d), approval_buttons(d["id"]))
            state.save_draft(d, state_dir)
            print(f"[{site.id}] drafted {post['slug']} ({d['id']}) with {model}")
            return True
        state.record_failed_topic(site.id, topic.title, attempts[-1] if attempts else "", state_dir)

    detail = "\n".join(f"• {esc(a)}" for a in attempts)
    print(f"::error title=[{site.name}] 자동 복구 실패::{' | '.join(attempts)}"[:1500].replace("\n", " "))
    tg.send(f"🚫 <b>[{esc(site.name)}]</b> 오늘 글을 만들지 못했습니다. Haiku → Sonnet 순서로 자동 재작성까지 시도했지만 실패해, "
            f"정해진 한도(글 1개당 {BUDGET_KRW}원, 마지막 시도 마무리는 최대 {HARD_LIMIT_KRW}원)에서 작업을 멈췄습니다. 이번에 쓴 비용: 약 {guard.spent_krw}원.\n"
            f"이 주제는 2주간 건너뛰고, 다음 예약 실행 때 다른 주제로 다시 시도합니다.\n{detail}")
    return False


def publish_draft(d: dict, *, github_transport=None, today: str | None = None) -> str:
    site = load_site(d["site"])
    fmt = get_format(site.format)
    post = dict(d["post"])
    # UTC date: the site build hides posts dated after its (UTC) build date.
    post["published"] = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    publisher = get_publisher(site.publisher, transport=github_transport)
    errors = fmt["validate"](post, site, publisher.existing_slugs())
    if errors:
        raise RuntimeError("; ".join(errors))
    filename, content = fmt["serialize"](post)
    url = publisher.publish(filename, content, f"Add blog post: {post['title']}")
    d["post"] = post
    return url


def run_poll(*, state_dir: Path = state.STATE_DIR, telegram: Telegram | None = None, github_transport=None,
             wait: int = 0) -> int:
    """Process button presses once. Returns how many updates were received."""
    tg = telegram or Telegram()
    offset = state.get_offset(state_dir)
    updates = tg.updates(offset, wait=wait)
    if updates or not wait:
        print(f"::notice title=Telegram::{len(updates)} button press(es) received")
    for update in updates:
        offset = max(offset, update["update_id"] + 1)
        try:
            _handle_update(update, tg, state_dir, github_transport)
        except Exception as e:  # noqa: BLE001 — one bad update must not block the others
            report_error(tg, "버튼 처리 오류", e)
    state.set_offset(offset, state_dir)
    return len(updates)


def has_pending(state_dir: Path = state.STATE_DIR, max_age_hours: float = 24) -> bool:
    """Is a recent draft waiting for a decision? Old undecided drafts are left to the hourly poll."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return any(d["status"] == "pending" and d["created"] >= cutoff for d in state.all_drafts(state_dir))


def run_listen(minutes: float, *, state_dir: Path = state.STATE_DIR, telegram: Telegram | None = None,
               github_transport=None, clock=time.monotonic) -> None:
    """Wait for Approve / Reject presses and handle each within seconds.

    Runs right after drafting, so the owner gets an immediate result instead of depending on
    GitHub's best-effort schedule. Stops as soon as no draft is pending, or after `minutes`.
    """
    tg = telegram or Telegram()
    deadline = clock() + minutes * 60
    while has_pending(state_dir) and clock() < deadline:
        wait = int(max(1, min(50, deadline - clock())))
        run_poll(state_dir=state_dir, telegram=tg, github_transport=github_transport, wait=wait)
    print("listen: done" if not has_pending(state_dir) else "listen: time limit reached, drafts still pending")


def _answer(tg: Telegram, callback_id: str, text: str) -> None:
    """Telegram rejects answers to buttons pressed long ago; that must not stop processing."""
    try:
        tg.answer(callback_id, text)
    except Exception as e:  # noqa: BLE001
        print(f"answerCallbackQuery skipped: {e}")


def _handle_update(update: dict, tg: Telegram, state_dir: Path, github_transport) -> None:
    cq = update.get("callback_query")
    if not cq or ":" not in cq.get("data", ""):
        print(f"update {update['update_id']}: not a button press, ignored")
        return
    if str(cq.get("message", {}).get("chat", {}).get("id")) != str(tg.chat_id):
        # Chat ids are not printed: Actions logs of a public repo are public.
        print(f"::warning title=다른 채팅의 버튼::update {update['update_id']} came from a chat that is not TELEGRAM_CHAT_ID")
        _answer(tg, cq["id"], "이 채팅에서는 승인할 수 없습니다.")
        return
    action, draft_id = cq["data"].split(":", 1)
    d = state.load_draft(draft_id, state_dir)
    if d is None or d["status"] != "pending":
        _answer(tg, cq["id"], "이미 처리된 글입니다.")
        return
    site = load_site(d["site"])
    message_id = d.get("telegram_message_id") or cq["message"]["message_id"]
    base = format_review_message(site, d)
    if action == "reject":
        d["status"], d["decided"] = "rejected", state.now_iso()
        state.save_draft(d, state_dir)
        _answer(tg, cq["id"], "폐기했습니다.")
        tg.edit(message_id, base + "\n\n❌ <b>폐기됨</b>")
        print(f"rejected {d['post']['slug']}")
    elif action == "approve":
        try:
            url = publish_draft(d, github_transport=github_transport)
        except Exception as e:  # noqa: BLE001
            _answer(tg, cq["id"], "발행 실패")
            report_error(tg, f"[{site.name}] 발행 실패 — {d['post']['title']}", e)
            return
        d["status"], d["decided"], d["published_file"] = "published", state.now_iso(), url
        state.save_draft(d, state_dir)
        _answer(tg, cq["id"], "발행했습니다.")
        live = f"{site.url}/blog/{d['post']['slug']}"
        tg.edit(message_id, base + f'\n\n✅ <b>발행됨</b> — 배포 후 몇 분 뒤 <a href="{live}">여기</a>에서 보입니다.')
        print(f"published {d['post']['slug']}")


def run_check(*, require_calculator: bool = False) -> None:
    for site_id in all_site_ids():
        site = load_site(site_id)
        get_format(site.format)
        get_publisher(site.publisher)
        note = ""
        if site.calculator:
            missing = [t.title for t in site.topics if not t.calculations]
            if missing:
                raise ValueError(f"[{site.id}] topics without calculations: {missing}")
            try:
                # Every hand-written topic's calculator input must be valid for the site's real code.
                for t in site.topics:
                    calculator.run(site, list(t.calculations))
                note = ", all topic calculations valid"
            except calculator.CalculatorError as e:
                if require_calculator or "not found" not in str(e):
                    raise
                note = " (calculator not available here, calculations not checked)"
        print(f"[{site.id}] ok: {len(site.tools)} tools, {len(site.topics)} topics{note}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="engine")
    sub = parser.add_subparsers(dest="command", required=True)
    p_draft = sub.add_parser("draft")
    p_draft.add_argument("--site", help="site id (default: all sites)")
    p_draft.add_argument("--extra", type=int, default=0, help="draft this many posts beyond today's quota")
    sub.add_parser("poll")
    p_listen = sub.add_parser("listen")
    p_listen.add_argument("--minutes", type=float, default=240)
    p_check = sub.add_parser("check")
    p_check.add_argument("--require-calculator", action="store_true", help="fail if a site's calculator cannot run")
    sub.add_parser("notify")
    args = parser.parse_args(argv)

    if args.command == "notify":
        # Best-effort: never fails the workflow and never sends Telegram messages.
        from engine.indexnow import run_notify
        try:
            run_notify()
        except Exception as e:  # noqa: BLE001
            print(f"::warning title=IndexNow::{e}")
        return 0

    try:
        if args.command == "draft":
            return 1 if run_draft([args.site] if args.site else all_site_ids(), extra=args.extra) else 0
        if args.command == "poll":
            run_poll()
        elif args.command == "listen":
            run_listen(args.minutes)
        elif args.command == "check":
            run_check(require_calculator=args.require_calculator)
    except Exception as e:  # noqa: BLE001
        report_error(Telegram(), f"엔진 {args.command} 오류", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
