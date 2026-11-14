"""Pinterest pin drafts: sent once per live post, only for enabled sites, never breaking anything."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from engine import state
from engine.config import load_site
from engine.pinterest import TAG, answer_lines, in_send_window, pin_text, run_pins, send_hours
from engine.telegram import Telegram

CONCRETE_FIELDS = [{"calculator": "concrete-calculator", "fields": [
    {"kind": "number", "key": "lengthFt", "label": "Length", "unit": "ft"},
    {"kind": "select", "key": "bagLb", "label": "Bag size", "options": [
        {"value": "60", "label": "60 lb (0.45 cu ft)"}, {"value": "80", "label": "80 lb (0.6 cu ft)"}]},
]}]


def calc(bag: str, bags: int) -> dict:
    return {"calculator": "concrete-calculator", "input": {"lengthFt": 10, "bagLb": bag},
            "results": [{"display": f"{bags} bags", "primary": True}, {"display": "1.23 cu yd", "primary": False}]}


def post(slug="slab", tool="concrete-calculator") -> dict:
    return {"slug": slug, "title": "How many bags of concrete for a 10x10 slab?",
            "description": "A 10x10 slab needs 56 bags of 80 lb mix.", "relatedTool": tool,
            "_calc_results": [calc("80", 56), calc("60", 75)], "_calc_fields": CONCRETE_FIELDS}


class FakeTelegram(Telegram):
    def __init__(self, fail=False):
        super().__init__(transport=self._t)
        self.chat_id, self.fail, self.calls = "42", fail, []

    def _t(self, method, payload):
        if self.fail:
            raise RuntimeError("telegram down")
        self.calls.append((method, payload))
        return {"message_id": len(self.calls)}


class AnswerTests(unittest.TestCase):
    def test_names_the_differing_input(self):
        self.assertEqual(answer_lines(post()), ["56 bags (Bag size: 80 lb)", "75 bags (Bag size: 60 lb)"])

    def test_single_calculation_has_no_qualifier(self):
        p = post()
        p["_calc_results"] = p["_calc_results"][:1]
        self.assertEqual(answer_lines(p), ["56 bags"])

    def test_post_without_calculations(self):
        self.assertEqual(answer_lines({"slug": "x"}), [])

    def test_pin_text_uses_board_link_image_and_limits(self):
        site = load_site("homerivet")
        pin = pin_text(site, post())
        self.assertEqual(pin["board"], "Concrete Projects")
        self.assertEqual(pin["link"], "https://homerivettools.com/blog/slab")
        self.assertEqual(pin["image"], "https://homerivettools.com/pin/blog-slab.png")
        self.assertIn("Quick answer: 56 bags (Bag size: 80 lb); 75 bags (Bag size: 60 lb).", pin["description"])
        self.assertIn("product label", pin["description"])
        self.assertLessEqual(len(pin["description"]), 500)
        self.assertLessEqual(len(pin["title"]), 100)
        self.assertEqual(pin_text(site, post(tool="square-footage-calculator"))["board"], "Home Project Calculators")


class RunPinsTests(unittest.TestCase):
    NOW = datetime(2026, 11, 5, tzinfo=timezone.utc)  # 09:00 Korean time: inside the morning window

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        drafts = [
            ("h0", "homerivet", "published", "2026-11-04T10:00:00Z"),
            ("h1", "homerivet", "published", "2026-10-01T10:00:00Z"),  # too old
            ("h2", "homerivet", "rejected", "2026-11-04T10:00:00Z"),
            ("d0", "devrivet", "published", "2026-11-04T10:00:00Z"),   # Pinterest not enabled
        ]
        for id_, site, status, decided in drafts:
            state.save_draft({"id": id_, "site": site, "status": status, "decided": decided, "created": decided,
                              "review": {"title_ko": "10x10 콘크리트"}, "post": post(slug=f"post-{id_}")}, self.tmp)

    def run_pins(self, tg, page=200, image=200, raises=False, now=None):
        def http(method, url, body=None):
            if raises:
                raise OSError("network down")
            return (image if url.endswith(".png") else page), ""
        return run_pins(state_dir=self.tmp, http=http, telegram=tg, now=now or self.NOW)

    def test_sends_once_for_recent_live_enabled_post(self):
        tg = FakeTelegram()
        self.assertEqual(self.run_pins(tg), 1)
        methods = [m for m, _ in tg.calls]
        self.assertEqual(methods, ["sendPhoto", "sendMessage"])
        self.assertEqual(tg.calls[0][1]["photo"], "https://homerivettools.com/pin/blog-post-h0.png")
        text = tg.calls[1][1]["text"]
        self.assertIn(TAG, text)
        self.assertIn("10x10 콘크리트", text)
        self.assertNotIn("reply_markup", tg.calls[1][1])  # no buttons: never confused with post approval
        self.assertTrue(state.load_draft("h0", self.tmp)["pin_sent"])
        for id_ in ("h1", "h2", "d0"):
            self.assertNotIn("pin_sent", state.load_draft(id_, self.tmp), id_)
        tg2 = FakeTelegram()
        self.assertEqual(self.run_pins(tg2), 0)
        self.assertEqual(tg2.calls, [])

    def test_waits_until_page_and_image_are_live(self):
        for kwargs in ({"page": 404}, {"image": 404}):
            tg = FakeTelegram()
            self.assertEqual(self.run_pins(tg, **kwargs), 0, kwargs)
            self.assertEqual(tg.calls, [], kwargs)
            self.assertNotIn("pin_sent", state.load_draft("h0", self.tmp), kwargs)

    def test_only_sends_in_the_korean_morning_window(self):
        kst = timezone(timedelta(hours=9))
        for hour in (0, 6, 12, 15, 23):  # outside 07:00-11:59 Korean time
            tg = FakeTelegram()
            self.assertEqual(self.run_pins(tg, now=datetime(2026, 11, 5, hour, 30, tzinfo=kst)), 0, hour)
            self.assertEqual(tg.calls, [], hour)
        self.assertNotIn("pin_sent", state.load_draft("h0", self.tmp))
        for hour in (7, 11):
            self.assertTrue(in_send_window(load_site("homerivet"), datetime(2026, 11, 5, hour, 59, tzinfo=kst)), hour)
        # The next morning it goes out (still within GIVE_UP_DAYS).
        self.assertEqual(self.run_pins(FakeTelegram(), now=datetime(2026, 11, 6, 7, 23, tzinfo=kst)), 1)

    def test_failures_never_raise_and_are_retried(self):
        self.assertEqual(self.run_pins(FakeTelegram(), raises=True), 0)
        self.assertEqual(self.run_pins(FakeTelegram(fail=True)), 0)
        self.assertNotIn("pin_sent", state.load_draft("h0", self.tmp))
        self.assertEqual(self.run_pins(FakeTelegram()), 1)  # works again once Telegram is back

    def test_cli_pins_always_succeeds(self):
        from engine import cli
        with mock.patch("engine.pinterest.run_pins", side_effect=RuntimeError("boom")):
            self.assertEqual(cli.main(["pins"]), 0)


class SiteConfigTests(unittest.TestCase):
    def test_only_homerivet_has_pinterest(self):
        self.assertTrue(load_site("homerivet").pinterest["enabled"])
        self.assertIsNone(load_site("devrivet").pinterest)

    def test_homerivet_sends_in_the_morning(self):
        start, end = send_hours(load_site("homerivet"))
        self.assertEqual((start, end), (7, 12))
        self.assertTrue(0 <= start < end <= 24)

    def test_every_board_has_a_korean_name(self):
        p = load_site("homerivet").pinterest
        tool_slugs = {t.slug for t in load_site("homerivet").tools}
        for slug, board in p["boards"].items():
            self.assertIn(slug, tool_slugs)
            self.assertIn(board, p["boards_ko"])
        self.assertIn(p["default_board"], p["boards_ko"])


if __name__ == "__main__":
    unittest.main()
