#!/usr/bin/env bash
# flake0-classify — ask a model whether this run's failed jobs failed from flakiness.
# Runs in its own job after the test jobs: a job's log is only served once the
# job has finished. Must never fail the calling job, like plot. Hence no `set -e`.
set -uo pipefail

OUTDIR="${RUNNER_TEMP:-/tmp}/flake0-classify"
ACTION_PATH="${GITHUB_ACTION_PATH:-$(cd "$(dirname "$0")/.." && pwd)}"
REPO="${GITHUB_REPOSITORY:?}"
RUN_ID="${GITHUB_RUN_ID:?}"
RUN_ATTEMPT="${GITHUB_RUN_ATTEMPT:-1}"

emit_output() {
  [ -n "${GITHUB_OUTPUT:-}" ] && echo "$1=$2" >> "$GITHUB_OUTPUT"
  return 0
}

skip() {
  echo "flake0-classify: $1, skipping" >&2
  emit_output verdicts-path ""
  exit 0
}

[ -n "${OPENROUTER_API_KEY:-}" ] || skip "no OpenRouter API key (fork pull requests get no secrets)"

WORK="$(mktemp -d "${RUNNER_TEMP:-/tmp}/flake0-classify-work.XXXXXX")"
mkdir -p "$WORK/logs" "$WORK/zips" "$WORK/bundles" "$OUTDIR"

# FLAKE0_CLASSIFY_NAME_RE is for this repo's own CI, whose demo test fails
# inside a passing job: it selects finished jobs by name instead of failed ones.
if ! gh api --paginate "repos/$REPO/actions/runs/$RUN_ID/attempts/$RUN_ATTEMPT/jobs" \
  | jq -c --arg re "${FLAKE0_CLASSIFY_NAME_RE:-}" '.jobs[]
      | select(if $re == "" then .conclusion == "failure" or .conclusion == "timed_out" else .status == "completed" and (.name | test($re)) end)
      | {id, name, runner_name}' > "$WORK/jobs.ndjson"; then
  skip "could not list this run's jobs (does the job have 'actions: read'?)"
fi
[ -s "$WORK/jobs.ndjson" ] || skip "no failed jobs in this run"

while IFS= read -r id; do
  # On failure gh writes the error body to stdout: drop it rather than send it as the log.
  if ! gh api --allow-escape-sequences "repos/$REPO/actions/jobs/$id/logs" > "$WORK/logs/$id.log"; then
    rm -f "$WORK/logs/$id.log"
    echo "flake0-classify: could not fetch the log of job $id" >&2
  fi
done < <(jq -r .id "$WORK/jobs.ndjson")

# By id, not `gh run download`: matrix legs can upload artifacts with the same name.
while IFS= read -r aid; do
  if gh api "repos/$REPO/actions/artifacts/$aid/zip" > "$WORK/zips/$aid.zip" \
    && unzip -q -o "$WORK/zips/$aid.zip" -d "$WORK/zips/$aid"; then
    for bundle in "$WORK/zips/$aid"/flake0-bundle.tar.*; do
      [ -e "$bundle" ] || continue
      mkdir -p "$WORK/bundles/$aid"
      tar -xf "$bundle" -C "$WORK/bundles/$aid" 2>/dev/null \
        || echo "flake0-classify: could not extract the bundle of artifact $aid" >&2
    done
    rm -rf "$WORK/zips/$aid" "$WORK/zips/$aid.zip"
  else
    echo "flake0-classify: could not download artifact $aid" >&2
  fi
done < <(gh api --paginate "repos/$REPO/actions/runs/$RUN_ID/artifacts" \
  | jq -r '.artifacts[] | select(.name | startswith("flake0-")) | .id')

# A bundle comes from the test job, which can run pull request code. Keep only
# regular files: a symlink to /proc/self/environ would put this job's secrets
# in the prompt, and a FIFO would hang the read.
find "$WORK/bundles" ! -type f ! -type d -delete

# FLAKE0_CLASSIFY_NO_DIFF is for this repo's own CI, where the diff of a pull
# request that touches its flaky-by-design check would give the answer away.
if [ -n "${PR_NUMBER:-}" ] && [ -z "${FLAKE0_CLASSIFY_NO_DIFF:-}" ]; then
  gh pr diff "$PR_NUMBER" -R "$REPO" > "$WORK/diff.patch" \
    || echo "flake0-classify: could not fetch the diff of #$PR_NUMBER" >&2
fi

RC=0
python3 "$ACTION_PATH/scripts/classify.py" --jobs "$WORK/jobs.ndjson" --logs "$WORK/logs" \
  --bundles "$WORK/bundles" --attempt "$RUN_ATTEMPT" --diff "$WORK/diff.patch" --out "$OUTDIR" \
  --model "${MODEL:-}" || RC=$?
[ "$RC" -eq 0 ] || echo "flake0-classify: classify.py exited $RC" >&2

if [ -s "$OUTDIR/verdicts.json" ]; then
  emit_output verdicts-path "$OUTDIR/verdicts.json"
else
  emit_output verdicts-path ""
fi
exit 0
