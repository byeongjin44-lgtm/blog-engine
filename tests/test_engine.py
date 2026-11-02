import json
import tempfile
import unittest
from pathlib import Path

from engine import state
from engine.cli import format_review_message, run_draft, run_poll
from engine.config import load_site
from engine.formats import serialize_devrivet, validate_devrivet, word_count
from engine.http import HttpError
from engine.telegram import Telegram

SITE = load_site("devrivet")


def make_post(slug="cron-every-monday", words=800, link=True, extra=None):
    filler = " ".join(["word"] * (words - 40))
    body = [
        {"type": "p", "text": "Use `0 0 * * 1` to run every Monday at midnight."},
        {"type": "p", "text": ("Try it in the [Cron Expression Generator](/cron-generator). " if link else "") + filler},
        {"type": "h2", "text": "FAQ"},
        {"type": "h3", "text": "Is 0 Sunday?"},
        {"type": "p", "text": "Yes, 0 and 7 both mean Sunday in most cron implementations."},
        {"type": "code", "text": "0 0 * * 1"},
    ]
    post = {
        "slug": slug,
        "title": "Cron expression for every Monday",
        "description": "Run a cron job every Monday with 0 0 * * 1. Learn day-of-week numbering and common weekly schedules.",
        "relatedTool": "cron-generator",
        "body": body + (extra or []),
    }
    return post


class FakeLLM:
    """Answers submit_post with queued posts and submit_review with queued reviews."""

    def __init__(self, posts, reviews, topics=None):
        self.posts, self.reviews, self.calls = list(posts), list(reviews), []
        self.topics = list(topics or [])

    def __call__(self, payload):
        schema = payload["output_config"]["format"]["schema"]
        props = schema["properties"]
        name = "submit_review" if "summary_ko" in props else "propose_topics" if "topics" in props else "submit_post"
        self.calls.append((name, payload))
        self.check_strict(schema)
        if name == "propose_topics":
            data = {"topics": self.topics.pop(0) if self.topics else []}
        else:
            data = self.posts.pop(0) if name == "submit_post" else self.reviews.pop(0)
        if data == "TRUNCATED":
            return {"stop_reason": "max_tokens", "content": [{"type": "text", "text": '{"slug": "cut'}]}
        return {"stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps(data)}]}

    @classmethod
    def check_strict(cls, node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False, node
            for value in node.values():
                cls.check_strict(value)
        elif isinstance(node, list):
            for item in node:
                cls.check_strict(item)


OK_REVIEW = {"title_ko": "매주 월요일 cron 표현식", "summary_ko": ["요약 1", "요약 2"], "issues": [], "verdict": "ok"}


class FakeTelegram(Telegram):
    def __init__(self, updates=None):
        super().__init__(transport=self._t)
        self.chat_id = "42"
        self.sent, self.edits, self.answers = [], [], []
        self._updates = updates or []

    def _t(self, method, payload):
        if method == "sendMessage":
            self.sent.append(payload)
            return {"message_id": len(self.sent)}
        if method == "editMessageText":
            self.edits.append(payload)
        if method == "answerCallbackQuery":
            self.answers.append(payload)
        if method == "getUpdates":
            return [u for u in self._updates if u["update_id"] >= payload["offset"]]
        return True


class FakeGitHub:
    def __init__(self, existing=()):
        self.existing, self.puts = set(existing), []

    def __call__(self, method, url, payload):
        if method == "GET":
            return [{"name": f"{s}.json", "type": "file"} for s in self.existing]
        self.puts.append((url, payload))
        return {"content": {"html_url": "https://github.com/x/y/blob/master/content/blog/file.json"}}


class FormatTests(unittest.TestCase):
    def test_valid_post(self):
        self.assertEqual(validate_devrivet(make_post(), SITE, set()), [])

    def test_detects_problems(self):
        errors = validate_devrivet(make_post(words=100, link=False), SITE, set())
        self.assertTrue(any("too short" in e for e in errors))
        self.assertTrue(any("must link to its tool" in e for e in errors))

    def test_duplicate_slug_and_unknown_link(self):
        post = make_post(extra=[{"type": "p", "text": "See [this](/not-a-page)."}])
        errors = validate_devrivet(post, SITE, {"cron-every-monday"})
        self.assertIn("slug 'cron-every-monday' already exists", errors)
        post["slug"] = "other"
        self.assertTrue(any("unknown page /not-a-page" in e for e in validate_devrivet(post, SITE, set())))

    def test_schema_errors(self):
        post = make_post(extra=[{"type": "html", "text": "<b>x</b>"}])
        self.assertIn("body[6] has unknown type 'html'", validate_devrivet(post, SITE, set()))

    def test_banned_phrase(self):
        post = make_post(extra=[{"type": "p", "text": "Let's delve into cron."}])
        self.assertTrue(any("banned phrase" in e for e in validate_devrivet(post, SITE, set())))

    def test_word_count_ignores_markup(self):
        self.assertEqual(word_count([{"type": "p", "text": "Use the [Cron tool](/cron-generator) `now`"}]), 5)

    def test_serialize_matches_site_schema_order(self):
        post = make_post()
        post["published"] = "2026-11-01"
        name, content = serialize_devrivet(post)
        self.assertEqual(name, "cron-every-monday.json")
        data = json.loads(content)
        self.assertEqual(list(data)[:5], ["slug", "title", "description", "published", "relatedTool"])


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_draft_then_approve_publishes(self):
        llm = FakeLLM([make_post()], [OK_REVIEW])
        tg, gh = FakeTelegram(), FakeGitHub(existing={"cron-every-5-minutes"})
        self.assertEqual(run_draft(["devrivet"], state_dir=self.tmp, telegram=tg, llm_transport=llm, github_transport=gh), 0)

        drafts = state.all_drafts(self.tmp)
        self.assertEqual(len(drafts), 1)
        d = drafts[0]
        self.assertEqual(d["status"], "pending")
        self.assertIn("새 글 검토", tg.sent[0]["text"])
        self.assertEqual(tg.sent[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"], f"approve:{d['id']}")
        # Existing site posts are offered to the writer for internal links.
        self.assertIn("/blog/cron-every-5-minutes", llm.calls[0][1]["messages"][0]["content"])

        # Second draft run the same day does nothing (posts_per_day = 1).
        run_draft(["devrivet"], state_dir=self.tmp, telegram=tg, llm_transport=FakeLLM([], []), github_transport=gh)
        self.assertEqual(len(state.all_drafts(self.tmp)), 1)

        update = {"update_id": 7, "callback_query": {"id": "cb1", "data": f"approve:{d['id']}",
                  "message": {"message_id": 1, "chat": {"id": 42}}}}
        tg2 = FakeTelegram([update])
        run_poll(state_dir=self.tmp, telegram=tg2, github_transport=gh)

        self.assertEqual(len(gh.puts), 1)
        url, payload = gh.puts[0]
        self.assertTrue(url.endswith("/repos/byeongjin44-lgtm/devrivet/contents/content/blog/cron-every-monday.json"))
        self.assertEqual(payload["branch"], "master")
        self.assertEqual(state.load_draft(d["id"], self.tmp)["status"], "published")
        self.assertIn("발행됨", tg2.edits[0]["text"])
        self.assertEqual(state.get_offset(self.tmp), 8)

        # Pressing the button again does not publish twice.
        run_poll(state_dir=self.tmp, telegram=FakeTelegram([dict(update, update_id=8)]), github_transport=gh)
        self.assertEqual(len(gh.puts), 1)

    def test_reject_and_foreign_chat(self):
        tg, gh = FakeTelegram(), FakeGitHub()
        run_draft(["devrivet"], state_dir=self.tmp, telegram=tg, llm_transport=FakeLLM([make_post()], [OK_REVIEW]), github_transport=gh)
        d = state.all_drafts(self.tmp)[0]
        foreign = {"update_id": 1, "callback_query": {"id": "x", "data": f"approve:{d['id']}", "message": {"message_id": 1, "chat": {"id": 999}}}}
        reject = {"update_id": 2, "callback_query": {"id": "y", "data": f"reject:{d['id']}", "message": {"message_id": 1, "chat": {"id": 42}}}}
        run_poll(state_dir=self.tmp, telegram=FakeTelegram([foreign, reject]), github_transport=gh)
        self.assertEqual(gh.puts, [])
        self.assertEqual(state.load_draft(d["id"], self.tmp)["status"], "rejected")
        # A rejected topic becomes available again.
        self.assertNotIn(d["topic"]["title"], state.used_topics("devrivet", self.tmp))

    def test_invalid_draft_is_retried_with_feedback_then_reported(self):
        bad = make_post(words=100, link=False)
        llm = FakeLLM([bad, bad], [])
        tg = FakeTelegram()
        failures = run_draft(["devrivet"], state_dir=self.tmp, telegram=tg, llm_transport=llm, github_transport=FakeGitHub())
        self.assertEqual(failures, 1)
        self.assertEqual(state.all_drafts(self.tmp), [])
        self.assertIn("오늘 글을 만들지 못했습니다", tg.sent[0]["text"])
        # Haiku then Sonnet on one topic only; that topic is parked for two weeks.
        self.assertEqual(len(state.recently_failed_topics("devrivet", state_dir=self.tmp)), 1)
        self.assertIn("too short", llm.calls[1][1]["messages"][0]["content"])

    def test_cut_off_post_is_retried_shorter(self):
        llm = FakeLLM(["TRUNCATED", make_post()], [OK_REVIEW])
        failures = run_draft(["devrivet"], state_dir=self.tmp, telegram=FakeTelegram(), llm_transport=llm, github_transport=FakeGitHub())
        self.assertEqual(failures, 0)
        self.assertEqual(state.all_drafts(self.tmp)[0]["status"], "pending")
        self.assertIn("got cut off", llm.calls[1][1]["messages"][0]["content"])

    def test_high_severity_review_triggers_revision(self):
        high = {**OK_REVIEW, "issues": [{"severity": "high", "ko": "표현식 오류", "en": "Expression is wrong"}], "verdict": "fix"}
        revised = make_post()
        revised["title"] = "Cron expression for every Monday (revised)"
        llm = FakeLLM([make_post(), revised], [high, OK_REVIEW])
        run_draft(["devrivet"], state_dir=self.tmp, telegram=FakeTelegram(), llm_transport=llm, github_transport=FakeGitHub())
        d = state.all_drafts(self.tmp)[0]
        self.assertEqual(d["post"]["title"], "Cron expression for every Monday (revised)")
        self.assertEqual(d["review"]["issues"], [])
        self.assertIn("Expression is wrong", llm.calls[2][1]["messages"][0]["content"])

    def test_review_message_escapes_html(self):
        d = {"id": "abc", "topic": {"keyword": "a<b"}, "post": {"title": "x & y"},
             "review": {"title_ko": "제목", "summary_ko": ["<script>"], "issues": [], "word_count": 900}}
        text = format_review_message(SITE, d)
        self.assertIn("x &amp; y", text)
        self.assertIn("&lt;script&gt;", text)


class ReviewFixTests(unittest.TestCase):
    def test_all_review_comments_are_applied_before_sending(self):
        low = {**OK_REVIEW, "issues": [{"severity": "low", "ko": "도입부가 장황함", "en": "Intro is wordy"}], "verdict": "fix"}
        revised = make_post()
        revised["title"] = "Cron expression for every Monday (tightened)"
        llm = FakeLLM([make_post(), revised], [low, OK_REVIEW])
        tg = FakeTelegram()
        run_draft(["devrivet"], state_dir=Path(tempfile.mkdtemp()), telegram=tg, llm_transport=llm, github_transport=FakeGitHub())
        self.assertIn("Intro is wordy", [c for c in llm.calls if c[0] == "submit_post"][1][1]["messages"][0]["content"])
        text = tg.sent[0]["text"]
        self.assertIn("검토 의견 1건 자동 반영 완료", text)
        self.assertIn("도입부가 장황함", text)
        self.assertIn("남은 문제 없음", text)

    def test_review_rounds_are_locked_at_two(self):
        from engine import writer
        self.assertEqual(writer.REVIEW_ROUNDS, 2, "Owner decision: max 2 review rewrite rounds. Do not change without approval.")

    def test_stops_after_two_rounds_and_shows_what_is_left(self):
        nag = {**OK_REVIEW, "issues": [{"severity": "low", "ko": "예시 하나 더", "en": "Add one more example"}], "verdict": "fix"}
        llm = FakeLLM([make_post(), make_post(), make_post()], [nag, nag, nag])
        tg = FakeTelegram()
        run_draft(["devrivet"], state_dir=Path(tempfile.mkdtemp()), telegram=tg, llm_transport=llm, github_transport=FakeGitHub())
        self.assertEqual(len([c for c in llm.calls if c[0] == "submit_post"]), 3)  # first draft + 2 rounds
        self.assertIn("남은 의견", tg.sent[0]["text"])


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_falls_back_to_stronger_model(self):
        bad = make_post(words=100, link=False)
        llm = FakeLLM([bad, bad, make_post()], [OK_REVIEW])
        tg = FakeTelegram()
        self.assertEqual(run_draft(["devrivet"], state_dir=self.tmp, telegram=tg, llm_transport=llm, github_transport=FakeGitHub()), 0)
        d = state.all_drafts(self.tmp)[0]
        self.assertEqual(d["model"], SITE.fallback_model)
        self.assertEqual(len(d["recovered_from"]), 1)
        models = [c[1]["model"] for c in llm.calls if c[0] == "submit_post"]
        self.assertEqual(models, [SITE.writer_model, SITE.writer_model, SITE.fallback_model])
        self.assertIn("새 글 검토", tg.sent[0]["text"])

    def test_stops_after_haiku_and_sonnet_on_one_topic(self):
        bad = make_post(words=100, link=False)
        llm = FakeLLM([bad, bad, bad, bad, make_post()], [OK_REVIEW])
        tg = FakeTelegram()
        failures = run_draft(["devrivet"], state_dir=self.tmp, telegram=tg, llm_transport=llm,
                             github_transport=FakeGitHub(), extra=2)
        self.assertEqual(failures, 1)
        self.assertEqual(state.all_drafts(self.tmp), [])          # no next topic, no further posts today
        self.assertEqual(len([c for c in llm.calls if c[0] == "submit_post"]), 4)  # Haiku x2, Sonnet x2
        self.assertEqual(state.recently_failed_topics("devrivet", state_dir=self.tmp), {SITE.topics[0].title})
        self.assertIn("150원", tg.sent[-1]["text"])

    def test_owner_action_stops_and_explains_in_korean(self):
        from engine.errors import OwnerActionRequired

        def broke(payload):
            raise OwnerActionRequired("Claude API 크레딧이 모두 소진되었습니다.", "credit balance too low")

        tg = FakeTelegram()
        failures = run_draft(["devrivet"], state_dir=self.tmp, telegram=tg, llm_transport=broke, github_transport=FakeGitHub(), extra=2)
        self.assertEqual(failures, 1)  # stopped after the first: retrying cannot help
        self.assertEqual(len(tg.sent), 1)
        self.assertIn("직접 처리해 주셔야 할 일", tg.sent[0]["text"])
        self.assertIn("크레딧", tg.sent[0]["text"])
        self.assertEqual(state.recently_failed_topics("devrivet", state_dir=self.tmp), set())

    def test_extra_drafts_beyond_daily_quota(self):
        # The first two queued topics are both cron topics, which make_post() fits.
        llm = FakeLLM([make_post(), make_post(slug="cron-every-monday-2")], [OK_REVIEW, OK_REVIEW])
        run_draft(["devrivet"], state_dir=self.tmp, telegram=FakeTelegram(), llm_transport=llm, github_transport=FakeGitHub(), extra=1)
        self.assertEqual(len(state.all_drafts(self.tmp)), 2)

    def test_api_transport_retries_transient_errors_only(self):
        import os
        from unittest import mock
        from engine import llm as llm_mod
        from engine.errors import OwnerActionRequired

        calls = []

        def flaky(method, url, payload, headers, timeout):
            calls.append(1)
            if len(calls) < 3:
                raise HttpError(529, '{"error":{"type":"overloaded_error"}}', url)
            return {"ok": True}

        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}), mock.patch.object(llm_mod, "request_json", flaky):
            self.assertEqual(llm_mod._default_transport({}, sleep=lambda s: None), {"ok": True})
        self.assertEqual(len(calls), 3)

        def no_credit(method, url, payload, headers, timeout):
            raise HttpError(400, '{"error":{"message":"Your credit balance is too low"}}', url)

        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}), mock.patch.object(llm_mod, "request_json", no_credit):
            with self.assertRaises(OwnerActionRequired) as ctx:
                llm_mod._default_transport({}, sleep=lambda s: None)
        self.assertIn("크레딧", ctx.exception.message_ko)


class BudgetTests(unittest.TestCase):
    def test_owner_limits_are_locked(self):
        from engine import budget, cli
        msg = "Owner decision 2026-11-02: max 150 KRW per post, one topic, Haiku then Sonnet. Do not change without approval."
        self.assertEqual(budget.BUDGET_KRW, 150, msg)
        self.assertEqual(cli.MAX_TOPICS_PER_POST, 1, msg)

    def test_guard_caps_output_and_stops_at_budget(self):
        from engine.budget import BudgetExceeded, CostGuard

        seen = []

        def api(payload):
            seen.append(payload["max_tokens"])
            # Sonnet writing a long post: 3,000 input + 2,500 output tokens = about 40 KRW.
            return {"stop_reason": "end_turn", "usage": {"input_tokens": 3000, "output_tokens": 2500},
                    "content": [{"type": "text", "text": "{}"}]}

        guard = CostGuard(api)
        payload = {"model": "claude-sonnet-5-5", "max_tokens": 16000, "messages": [{"role": "user", "content": "x"}]}
        with self.assertRaises(BudgetExceeded):
            for _ in range(10):
                guard(payload)
        self.assertLessEqual(guard.spent_krw, 150)
        self.assertEqual(guard.calls, 3)                    # the 4th Sonnet call is refused before it is made
        self.assertTrue(all(m <= 16000 for m in seen))
        self.assertLess(seen[-1], 16000)                    # output was capped to what the budget allows

    def test_allowed_path_fits_the_budget(self):
        """Haiku fails twice, Sonnet writes, 2 review rewrite rounds: must complete under 150 KRW."""
        from engine.budget import CostGuard

        usage = {"claude-haiku-5-5": (2500, 2500), "claude-sonnet-5-5": (3000, 2500)}
        bad = make_post(words=100, link=False)
        posts = [bad, bad, make_post(), make_post(), make_post()]
        nag = {**OK_REVIEW, "issues": [{"severity": "low", "ko": "보완", "en": "improve"}]}
        reviews = [nag, nag, OK_REVIEW]

        def api(payload):
            schema = payload["output_config"]["format"]["schema"]
            data = reviews.pop(0) if "summary_ko" in schema["properties"] else posts.pop(0)
            i, o = usage[payload["model"]] if "summary_ko" not in schema["properties"] else (3000, 800)
            return {"stop_reason": "end_turn", "usage": {"input_tokens": i, "output_tokens": o},
                    "content": [{"type": "text", "text": json.dumps(data)}]}

        tmp = Path(tempfile.mkdtemp())
        run_draft(["devrivet"], state_dir=tmp, telegram=FakeTelegram(), llm_transport=api, github_transport=FakeGitHub())
        d = state.all_drafts(tmp)[0]
        self.assertEqual(d["model"], "claude-sonnet-5-5")
        self.assertEqual(len(d["review"]["applied"]), 2)
        self.assertLessEqual(d["cost_krw"], 150)


class ListenTests(unittest.TestCase):
    def test_listen_handles_press_and_stops_when_nothing_pending(self):
        from engine.cli import run_listen

        tmp = Path(tempfile.mkdtemp())
        gh = FakeGitHub()
        run_draft(["devrivet"], state_dir=tmp, telegram=FakeTelegram(), llm_transport=FakeLLM([make_post()], [OK_REVIEW]), github_transport=gh)
        d = state.all_drafts(tmp)[0]
        press = {"update_id": 3, "callback_query": {"id": "z", "data": f"approve:{d['id']}", "message": {"message_id": 1, "chat": {"id": 42}}}}
        tg = FakeTelegram([press])
        run_listen(240, state_dir=tmp, telegram=tg, github_transport=gh)
        self.assertEqual(state.load_draft(d["id"], tmp)["status"], "published")
        self.assertEqual(len(gh.puts), 1)

    def test_listen_respects_time_limit(self):
        from engine.cli import run_listen

        tmp = Path(tempfile.mkdtemp())
        run_draft(["devrivet"], state_dir=tmp, telegram=FakeTelegram(), llm_transport=FakeLLM([make_post()], [OK_REVIEW]), github_transport=FakeGitHub())
        ticks = iter(range(0, 100000, 60))
        run_listen(5, state_dir=tmp, telegram=FakeTelegram(), github_transport=FakeGitHub(), clock=lambda: next(ticks))
        self.assertEqual(state.all_drafts(tmp)[0]["status"], "pending")


class TopicRefillTests(unittest.TestCase):
    def test_refills_when_low_dedupes_and_notifies(self):
        from engine import topics as topic_queue

        tmp = Path(tempfile.mkdtemp())
        # Mark all but 3 hand-written topics as used.
        for i, t in enumerate(SITE.topics[:-3]):
            state.save_draft({"id": f"u{i}", "site": "devrivet", "status": "published", "created": "2026-01-01T00:00:00Z",
                              "topic": {"title": t.title, "keyword": t.keyword, "related_tool": t.related_tool},
                              "post": {"slug": f"s{i}", "title": t.title}, "review": {}}, tmp)
        proposals = [
            {"title": "Cron expression for every 15 minutes", "keyword": "cron every 15 minutes", "related_tool": "cron-generator",
             "notes": "", "title_ko": "15분마다 실행하는 cron"},
            {"title": SITE.topics[0].title, "keyword": "dup", "related_tool": "cron-generator", "notes": "", "title_ko": "중복"},
            {"title": "Unknown tool topic", "keyword": "x y z", "related_tool": "image-resizer", "notes": "", "title_ko": "없는 도구"},
        ]
        llm = FakeLLM([], [], topics=[proposals])  # drafting itself fails here; only the refill matters
        tg = FakeTelegram()
        run_draft(["devrivet"], state_dir=tmp, telegram=tg, llm_transport=llm, github_transport=FakeGitHub())

        auto = topic_queue.load_auto("devrivet", tmp)
        self.assertEqual([t["title"] for t in auto], ["Cron expression for every 15 minutes"])
        self.assertIn("새 주제 1개", tg.sent[0]["text"])
        self.assertIn("15분마다 실행하는 cron", tg.sent[0]["text"])
        # Hand-written topics still come first, the new topic is queued after them.
        # (Drafting failed in this test, so the two topics it tried are parked.)
        left = topic_queue.remaining(SITE, tmp)
        self.assertEqual([t.title for t in left], [t.title for t in SITE.topics[-2:]] + ["Cron expression for every 15 minutes"])

    def test_no_refill_when_queue_is_long(self):
        llm = FakeLLM([make_post()], [OK_REVIEW])
        run_draft(["devrivet"], state_dir=Path(tempfile.mkdtemp()), telegram=FakeTelegram(), llm_transport=llm, github_transport=FakeGitHub())
        self.assertNotIn("propose_topics", [c[0] for c in llm.calls])


class PublisherTests(unittest.TestCase):
    def test_missing_folder_means_no_posts(self):
        from engine.publishers import get_publisher

        def transport(method, url, payload):
            raise HttpError(404, "Not Found", url)

        self.assertEqual(get_publisher(SITE.publisher, transport=transport).existing_slugs(), set())


if __name__ == "__main__":
    unittest.main()
