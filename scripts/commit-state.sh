#!/usr/bin/env bash
# Commit engine state (drafts, Telegram offset) back to the repo after a workflow run.
set -euo pipefail
git config user.name "blog-engine[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git add state
if git diff --cached --quiet; then
  echo "No state changes."
  exit 0
fi
git commit -m "state: $1"
for attempt in 1 2 3; do
  git pull --rebase --quiet && git push && exit 0
  sleep $((attempt * 5))
done
echo "Failed to push state" >&2
exit 1
