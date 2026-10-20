#!/usr/bin/env bash
# flake0-classify — ask a model whether this job failed from flakiness.
# Runs as a step at the end of the failed test job, from the tee'd test output
# and the collect bundle on disk: GitHub serves a job's own log only once the
# job has finished. Must never fail the calling job, like plot. Hence no `set -e`.
set -uo pipefail

OUTDIR="${RUNNER_TEMP:-/tmp}/flake0-classify"
ACTION_PATH="${GITHUB_ACTION_PATH:-$(cd "$(dirname "$0")/.." && pwd)}"
REPO="${GITHUB_REPOSITORY:?}"

emit_output() {
  [ -n "${GITHUB_OUTPUT:-}" ] && echo "$1=$2" >> "$GITHUB_OUTPUT"
  return 0
}

skip() {
  echo "flake0-classify: $1, skipping" >&2
  emit_output verdict-path ""
  exit 0
}

[ -n "${OPENROUTER_API_KEY:-}" ] || skip "no OpenRouter API key (fork pull requests get no secrets)"

WORK="$(mktemp -d "${RUNNER_TEMP:-/tmp}/flake0-classify-work.XXXXXX")"
mkdir -p "$WORK/bundle" "$OUTDIR"

# The test output and the bundle come from steps that can run pull request code
# in this job. Keep only regular files: a symlink to /proc/self/environ would
# put this step's secrets in the prompt, and a FIFO would hang the read.
if [ -n "${LOG_PATH:-}" ] && [ -f "$LOG_PATH" ] && [ ! -L "$LOG_PATH" ]; then
  cp "$LOG_PATH" "$WORK/test.log"
else
  echo "flake0-classify: no test output at '${LOG_PATH:-}'" >&2
fi
if [ -n "${BUNDLE_PATH:-}" ] && [ -f "$BUNDLE_PATH" ]; then
  tar -xf "$BUNDLE_PATH" -C "$WORK/bundle" 2>/dev/null \
    || echo "flake0-classify: could not extract the bundle" >&2
  find "$WORK/bundle" ! -type f ! -type d -delete
else
  echo "flake0-classify: no collect bundle at '${BUNDLE_PATH:-}'" >&2
fi

# FLAKE0_CLASSIFY_NO_DIFF is for this repo's own CI, where the diff of a pull
# request that touches its flaky-by-design check would give the answer away.
if [ -n "${PR_NUMBER:-}" ] && [ -z "${FLAKE0_CLASSIFY_NO_DIFF:-}" ]; then
  gh pr diff "$PR_NUMBER" -R "$REPO" > "$WORK/diff.patch" \
    || echo "flake0-classify: could not fetch the diff of #$PR_NUMBER" >&2
fi

RC=0
python3 "$ACTION_PATH/scripts/classify.py" --job "${JOB_NAME:-}" --log "$WORK/test.log" \
  --bundle "$WORK/bundle" --diff "$WORK/diff.patch" --out "$OUTDIR" --model "${MODEL:-}" || RC=$?
[ "$RC" -eq 0 ] || echo "flake0-classify: classify.py exited $RC" >&2

if [ -s "$OUTDIR/verdict.json" ]; then
  emit_output verdict-path "$OUTDIR/verdict.json"
else
  emit_output verdict-path ""
fi
exit 0
