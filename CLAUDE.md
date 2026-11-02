# CLAUDE.md

Multi-site blog automation engine. Each day it drafts posts with the Claude API, sends a Korean
summary to Telegram with Approve / Reject buttons, and publishes approved posts to the site.
The owner does not read English: the Korean summary and review are how posts get approved.

## Layout

- `engine/` — Python, **standard library only** (no pip install; runs as-is on GitHub Actions).
  - `config.py` loads `sites/<id>/site.toml` + `topics.toml`
  - `writer.py` drafting prompts, validation retry, Korean review; every review comment is applied (up to 2 rewrite rounds) before the owner sees the post — the owner cannot act on review notes
  - `formats.py` post formats (`devrivet-json` mirrors `src/lib/blog.ts` in the devrivet repo — keep in sync)
  - `publishers.py` delivery (`github_files` commits into the site repo)
  - `topics.py` topic queue: hand-written `topics.toml` first, then topics the engine proposes itself
    (`state/topics/<id>.json`) when fewer than 7 remain
  - `telegram.py`, `state.py`, `cli.py` (`python -m engine draft|poll|check`)
- `sites/<id>/` — one folder per site. Adding a site = adding a folder; no engine change unless it
  needs a new format or publisher.
- `state/drafts/<id>.json` — one file per draft (`pending | published | rejected`), committed by the workflows.
- `.github/workflows/` — `draft.yml` (daily; after drafting it runs `engine listen` for up to 4 h so
  button presses are handled within seconds — GitHub's cron is best-effort and can be hours late),
  `poll.yml` (hourly fallback), `test.yml`. Don't turn `listen` into a 24/7 loop: Actions is not a server.

## Recovery (no human or AI fix needed for ordinary failures)

- Transient API/network errors (429, 5xx, 529, timeouts) are retried with pauses (`llm.py`, `publishers.py`).
- A draft that fails (validation, cut-off, bad JSON…) is rewritten with `models.fallback` (Sonnet), then the
  next topic is tried; failed topics are parked for 14 days (`state/failed/<id>.json`). The owner hears
  about it only if every attempt failed.
- Problems only the owner can fix (API key, credit, GitHub token) raise `OwnerActionRequired` and send a
  Korean message saying exactly what to change; drafting stops for that site until then.

## Rules

- Never publish without an explicit Approve from Telegram. No auto-approve.
- Content must be genuinely useful and correct; no fake traffic, no mass low-quality pages (Google spam
  policy, AdSense invalid-traffic policy).
- Secrets only via environment variables / GitHub Actions secrets. Never commit them.
- Run `python -m unittest discover -s tests` and `python -m engine check` after any change.
