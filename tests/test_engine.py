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

    def __init__(self, posts, reviews):
        self.posts, self.reviews, self.calls = list(posts), list(reviews), []

    def __call__(self, payload):
        schema = payload["output_config"]["format"]["schema"]
        name = "submit_review" if "summary_ko" in schema["properties"] else "submit_post"
        self.calls.append((name, payload))
        self.check_strict(schema)
        data = self.posts.pop(0) if name == "submit_post" else self.reviews.pop(0)
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
        self.assertIn("초안 생성 실패", tg.sent[0]["text"])
        self.assertIn("too short", llm.calls[1][1]["messages"][0]["content"])

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


class PublisherTests(unittest.TestCase):
    def test_missing_folder_means_no_posts(self):
        from engine.publishers import get_publisher

        def transport(method, url, payload):
            raise HttpError(404, "Not Found", url)

        self.assertEqual(get_publisher(SITE.publisher, transport=transport).existing_slugs(), set())


if __name__ == "__main__":
    unittest.main()
