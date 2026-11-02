"""HomeRivet: numbers from calculator code, locked content rules, engine-owned sources."""

import json
import tempfile
import unittest
from pathlib import Path

from engine import calculator, state
from engine import topics as topic_queue
from engine.cli import format_review_message, run_draft, run_poll
from engine.config import load_site
from engine.formats import FORBIDDEN_TOPICS, find_forbidden, serialize_homerivet, validate_homerivet
from tests.test_engine import FakeGitHub, FakeLLM, FakeTelegram

SITE = load_site("homerivet")

FIELDS = [{"calculator": "paint-calculator", "fields": [
    {"kind": "number", "key": "lengthFt", "label": "Room length", "unit": "ft", "default": 12},
    {"kind": "number", "key": "widthFt", "label": "Room width", "unit": "ft", "default": 15},
    {"kind": "number", "key": "coats", "label": "Coats", "default": 2},
    {"kind": "number", "key": "sqFtPerGallon", "label": "Coverage per gallon", "unit": "sq ft", "default": 350},
]}]


def paint_results(inp):
    return {"calculator": "paint-calculator", "input": inp, "results": [
        {"key": "gallons", "label": "Paint to buy", "value": 3, "unit": "gallons", "digits": 0, "primary": True,
         "number": "3", "display": "3 gallons"},
        {"key": "gallonsExact", "label": "Exact amount", "value": 2.13, "unit": "gallons", "digits": 2, "primary": False,
         "number": "2.13", "display": "2.13 gallons"},
    ]}


class FakeCalculator:
    """Stands in for `node scripts/calc.mts` from the site repo."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv):
        arg = argv[-1]
        self.calls.append(arg)
        if arg == "--list":
            return json.dumps(FIELDS)
        reqs = json.loads(arg)
        out = []
        for r in reqs:
            if r["calculator"] != "paint-calculator" or r["input"].get("lengthFt") == "bad":
                return json.dumps({"error": "Room length must be a number."})
            out.append(paint_results(r["input"]))
        return json.dumps(out)


def make_post(slug="how-much-paint-for-a-12x12-room", words=760, text=None, extra=None):
    filler = " ".join(["word"] * (words - 60))
    body = [
        {"type": "p", "text": text or "A 12 by 12 ft bedroom needs 3 gallons of paint for two coats (2.13 gallons exactly)."},
        {"type": "p", "text": "Run your own numbers in the [Paint Calculator](/paint-calculator). " + filler},
        {"type": "h2", "text": "FAQ"},
        {"type": "h3", "text": "Does every paint cover the same area?"},
        {"type": "p", "text": "No. Check the coverage printed on the can before you buy."},
    ]
    return {"slug": slug, "title": "How much paint for a 12x12 room",
            "description": "A 12x12 bedroom with 8 ft walls needs 3 gallons of paint for two coats. See the steps and check your own room.",
            "relatedTool": "paint-calculator", "body": body + (extra or [])}


def finalized(post, calcs=({"calculator": "paint-calculator", "input": {"lengthFt": 12, "widthFt": 12}},)):
    post = dict(post)
    post["sources"] = [dict(s) for s in SITE.tool("paint-calculator").sources]
    post["calculations"] = [dict(c) for c in calcs]
    post["_calc_results"] = [paint_results(c["input"]) for c in calcs]
    post["_calc_fields"] = FIELDS
    return post


OK_REVIEW = {"title_ko": "12x12 방 페인트 양", "summary_ko": ["요약"], "issues": [], "verdict": "ok"}


class LockedRuleTests(unittest.TestCase):
    def test_forbidden_topics_are_kept(self):
        required = {
            "electrical": ["wiring", "circuit breaker", "electrical panel"],
            "structural": ["load-bearing", "load calculation", "joist span", "beam size", "footing design"],
            "gas": ["gas line", "gas pipe"],
            "roofing": ["roofing", "roof repair", "ladder safety"],
            "plumbing": ["plumbing installation"],
            "hazardous": ["asbestos", "lead paint", "mold removal"],
            "codes": ["building code"],
        }
        for topic, phrases in required.items():
            for phrase in phrases:
                self.assertIn(phrase, FORBIDDEN_TOPICS[topic])

    def test_forbidden_matching_like_the_site(self):
        self.assertEqual(find_forbidden("Check the Circuit  Breaker first"), ["electrical: circuit breaker"])
        self.assertIn("hazardous: lead paint", find_forbidden("Removing LEAD-PAINT safely"))
        self.assertEqual(find_forbidden("Gaslight and roofline are different words."), [])

    def test_site_requires_calculator_and_topics_have_calculations(self):
        self.assertIsNotNone(SITE.calculator)
        self.assertTrue(all(t.calculations for t in SITE.topics))
        self.assertTrue(all(t.sources for t in SITE.tools))


class FormatTests(unittest.TestCase):
    def test_valid_post(self):
        self.assertEqual(validate_homerivet(finalized(make_post()), SITE, set()), [])

    def test_headline_number_must_match_calculator(self):
        post = finalized(make_post(text="A 12 by 12 ft bedroom needs 4 gallons of paint for two coats."))
        post["description"] = "A 12x12 bedroom with 8 ft walls needs 4 gallons of paint for two coats. See the steps and check your room."
        self.assertTrue(any('"3 gallons"' in e for e in validate_homerivet(post, SITE, set())))

    def test_invented_decimal_is_rejected(self):
        post = finalized(make_post(extra=[{"type": "p", "text": "A bigger room needs 4.75 gallons."}]))
        self.assertTrue(any("4.75" in e for e in validate_homerivet(post, SITE, set())))

    def test_forbidden_topic_manufacturer_note_and_external_links(self):
        post = finalized(make_post(extra=[{"type": "p", "text": "Turn off the circuit breaker. See [this](https://example.com)."}]))
        errors = validate_homerivet(post, SITE, set())
        self.assertTrue(any("forbidden topic" in e for e in errors))
        self.assertTrue(any("external links" in e for e in errors))
        post = finalized(make_post())
        post["body"] = [b for b in post["body"] if "printed on the can" not in b.get("text", "")]
        self.assertTrue(any("manufacturer" in e for e in validate_homerivet(post, SITE, set())))

    def test_needs_calculations_and_sources(self):
        post = finalized(make_post())
        post["calculations"], post["sources"] = [], []
        errors = validate_homerivet(post, SITE, set())
        self.assertTrue(any("calculator run" in e for e in errors))
        self.assertTrue(any("sources" in e for e in errors))

    def test_serialize_has_site_fields_only(self):
        post = finalized(make_post())
        post["published"] = "2026-11-02"
        name, content = serialize_homerivet(post)
        data = json.loads(content)
        self.assertEqual(name, "how-much-paint-for-a-12x12-room.json")
        self.assertEqual(list(data), ["slug", "title", "description", "published", "relatedTool", "sources", "calculations", "body"])
        self.assertTrue(all(s["url"].startswith("https://") for s in data["sources"]))


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.calc = FakeCalculator()
        self._old = calculator.runner
        calculator.runner = self.calc

    def tearDown(self):
        calculator.runner = self._old

    def test_numbers_are_computed_before_writing_and_published_with_sources(self):
        llm = FakeLLM([make_post()], [OK_REVIEW])
        tg, gh = FakeTelegram(), FakeGitHub()
        self.assertEqual(run_draft(["homerivet"], state_dir=self.tmp, telegram=tg, llm_transport=llm, github_transport=gh), 0)
        prompt = llm.calls[0][1]["messages"][0]["content"]
        self.assertIn("Paint to buy: 3 gallons (HEADLINE", prompt)
        self.assertIn("Never calculate", prompt)
        self.assertIn("Room length: 12 ft", prompt)
        review_prompt = llm.calls[1][1]["messages"][0]["content"]
        self.assertIn("must come only from these calculator results", review_prompt)
        self.assertNotIn("_calc_results", review_prompt)
        self.assertIn("🧮 paint-calculator: 3 gallons", tg.sent[0]["text"])

        d = state.all_drafts(self.tmp)[0]
        self.assertEqual(d["topic"]["calculations"][0]["calculator"], "paint-calculator")
        update = {"update_id": 1, "callback_query": {"id": "cb", "data": f"approve:{d['id']}",
                  "message": {"message_id": 1, "chat": {"id": 42}}}}
        run_poll(state_dir=self.tmp, telegram=FakeTelegram([update]), github_transport=gh)
        url, payload = gh.puts[0]
        self.assertTrue(url.endswith("/repos/byeongjin44-lgtm/homerivettools/contents/content/blog/how-much-paint-for-a-12x12-room.json"))
        self.assertEqual(payload["branch"], "main")
        import base64
        published = json.loads(base64.b64decode(payload["content"]))
        self.assertEqual(published["calculations"], [{"calculator": "paint-calculator",
                                                      "input": {"lengthFt": 12, "widthFt": 12, "heightFt": 8, "doors": 1, "windows": 1}}])
        self.assertEqual(published["sources"][0]["title"], "Sherwin-Williams paint calculator")
        self.assertFalse(any(k.startswith("_") for k in published))

    def test_bad_calculator_input_stops_before_any_ai_call(self):
        from dataclasses import replace
        import engine.cli as cli

        topic = replace(SITE.topics[0], calculations=({"calculator": "paint-calculator", "input": {"lengthFt": "bad"}},))
        old = cli.load_site
        cli.load_site = lambda site_id: replace(SITE, topics=(topic,))
        try:
            llm, tg = FakeLLM([], []), FakeTelegram()
            run_draft(["homerivet"], state_dir=self.tmp, telegram=tg, llm_transport=llm, github_transport=FakeGitHub())
        finally:
            cli.load_site = old
        self.assertEqual([c[0] for c in llm.calls if c[0] != "propose_topics"], [])
        self.assertIn("계산기", tg.sent[-1]["text"])

    def test_proposed_topics_need_valid_calculations_and_allowed_subjects(self):
        proposals = [
            {"title": "How much paint for a 10x10 room", "keyword": "paint 10x10 room", "related_tool": "paint-calculator",
             "notes": "", "title_ko": "10x10 방 페인트",
             "calculations": [{"calculator": "paint-calculator", "inputs": [{"key": "lengthFt", "value": "10"}]}]},
            {"title": "Paint before replacing a circuit breaker", "keyword": "x", "related_tool": "paint-calculator",
             "notes": "", "title_ko": "금지", "calculations": [{"calculator": "paint-calculator", "inputs": []}]},
            {"title": "Paint a hallway", "keyword": "paint hallway", "related_tool": "paint-calculator", "notes": "",
             "title_ko": "잘못된 입력", "calculations": [{"calculator": "paint-calculator", "inputs": [{"key": "lengthFt", "value": "bad"}]}]},
            {"title": "Paint without numbers", "keyword": "paint no numbers", "related_tool": "paint-calculator", "notes": "",
             "title_ko": "계산 없음", "calculations": []},
        ]
        llm = FakeLLM([], [], topics=[proposals])
        fresh = topic_queue.propose(SITE, [], transport=llm)
        self.assertEqual([t["title"] for t in fresh], ["How much paint for a 10x10 room"])
        self.assertEqual(fresh[0]["calculations"], [{"calculator": "paint-calculator", "input": {"lengthFt": 10}}])
        schema = llm.calls[0][1]["output_config"]["format"]["schema"]
        self.assertIn("calculations", schema["properties"]["topics"]["items"]["required"])


class MessageTests(unittest.TestCase):
    def test_review_message_lists_calculator_results(self):
        d = {"id": "abc", "post": finalized(make_post()), "review": OK_REVIEW, "topic": {"keyword": "k"}}
        self.assertIn("글에 쓰인 계산기 결과", format_review_message(SITE, d))


if __name__ == "__main__":
    unittest.main()
