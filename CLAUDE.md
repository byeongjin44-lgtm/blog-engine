# CLAUDE.md

Multi-site blog automation engine. Each day it drafts posts with the Claude API, sends a Korean
summary to Telegram with Approve / Reject buttons, and publishes approved posts to the site.
The owner does not read English: the Korean summary and review are how posts get approved.

## Layout

- `engine/` — Python, **standard library only** (no pip install; runs as-is on GitHub Actions).
  - `config.py` loads `sites/<id>/site.toml` + `topics.toml`
  - `writer.py` drafting prompts, validation retry, Korean review; every review comment is applied (up to 2 rewrite rounds) before the owner sees the post — the owner cannot act on review notes
  - `formats.py` post formats (`devrivet-json` mirrors `src/lib/blog.ts` in the devrivet repo; `homerivet-json`
    mirrors `src/lib/blog.ts` + `src/lib/content-policy.ts` in the homerivettools repo — keep both in sync)
  - `calculator.py` runs a site's own calculator code (HomeRivet: `node vendor/homerivettools/scripts/calc.mts`;
    the workflows check that repo out) so every number in a post comes from code, never from the AI
  - `publishers.py` delivery (`github_files` commits into the site repo)
  - `topics.py` topic queue: hand-written `topics.toml` first, then topics the engine proposes itself
    (`state/topics/<id>.json`) when fewer than 7 remain
  - `indexnow.py` announces published posts to Bing & co. once live (`python -m engine notify`, hourly in
    `poll.yml`); best-effort, never fails a workflow. The site serves the key at `/<indexnow_key>.txt`
  - `telegram.py`, `state.py`, `cli.py` (`python -m engine draft|poll|check|notify`)
- `sites/<id>/` — one folder per site. Adding a site = adding a folder; no engine change unless it
  needs a new format or publisher.
- `state/drafts/<id>.json` — one file per draft (`pending | published | rejected`), committed by the workflows.
- `.github/workflows/` — `draft.yml` (daily; after drafting it runs `engine listen` for up to 4 h so
  button presses are handled within seconds — GitHub's cron is best-effort and can be hours late),
  `poll.yml` (hourly fallback), `test.yml`. Don't turn `listen` into a 24/7 loop: Actions is not a server.

## Recovery (no human or AI fix needed for ordinary failures)

- Transient API/network errors (429, 5xx, 529, timeouts) are retried with pauses (`llm.py`, `publishers.py`).
- A draft that fails (validation, cut-off, bad JSON…) is rewritten with `models.fallback` (Sonnet) — within
  the per-post budget below. If that fails too, the topic is parked for 14 days (`state/failed/<id>.json`),
  drafting stops for the day, and the owner gets one Korean message.
- Problems only the owner can fix (API key, credit, GitHub token) raise `OwnerActionRequired` and send a
  Korean message saying exactly what to change; drafting stops for that site until then.

## Rules

- **Per-post spending cap: 150 KRW (owner decision, 2026-11-02).** Allowed path per post: Haiku, then Sonnet
  (`models.fallback`), on ONE topic, with up to 2 review rewrite rounds. If that fails, stop for the day —
  no next topic, no more posts. Enforced by `engine/budget.py` (`CostGuard` caps `max_tokens` and tracks real
  usage), `MAX_TOPICS_PER_POST = 1` in `cli.py`, and locked by tests. Never change without explicit approval.
  **Finishing grace (owner decision, 2026-11-05):** a running attempt may finish slightly over 150 KRW so a nearly
  done post is still sent — never above `HARD_LIMIT_KRW` = 200 KRW, with 20 KRW of it kept for the Korean review.
  New work (the Sonnet attempt, review rewrite rounds via `guard.optional_work()`) only starts within 150 KRW.
  Writer output is sized to the longest allowed post (`writer_max_tokens`), so a runaway draft cannot eat the budget.
- **Review rewrite rounds are capped at 2 per post (owner decision, cost). Never raise the cap without the
  owner's explicit approval** — `REVIEW_ROUNDS` in `writer.py`, also hard-capped in the loop and locked by a test.

- **HomeRivet content rules are LOCKED (owner decision, 2026-11-01) — never loosen without explicit approval:**
  numbers come only from the calculator (topics carry `calculations`; the engine runs them before any AI call,
  headline results must appear exactly and other decimals must be known numbers), material quantities only
  (`FORBIDDEN_TOPICS` in `formats.py`, also filters proposed topics), sources are added by the engine from
  `site.toml` (no external links in the body), and every post points readers to the manufacturer's label.
  Locked by `tests/test_homerivet.py`.
- Never publish without an explicit Approve from Telegram. No auto-approve.
- Content must be genuinely useful and correct; no fake traffic, no mass low-quality pages (Google spam
  policy, AdSense invalid-traffic policy).
- Secrets only via environment variables / GitHub Actions secrets. Never commit them.
- Run `python -m unittest discover -s tests` and `python -m engine check` after any change
  (`--require-calculator` with the site repo at `vendor/homerivettools`, as the test workflow does).
