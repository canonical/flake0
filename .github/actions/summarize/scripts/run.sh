#!/usr/bin/env bash
# flake0-summarize — post or update the flake0 comment on the pull request.
# Each failed job runs it after classify (MODE=update), and one job runs it
# after all of them (MODE=final). Every run rebuilds the comment from all the
# verdict artifacts so far, so an edit lost to a concurrent job gets repaired.
# Must never fail the job, like classify. Hence no `set -e`.
set -uo pipefail

REPO="${GITHUB_REPOSITORY:?}"
VERDICTS="${RUNNER_TEMP:?}/flake0-verdicts"

skip() {
  echo "flake0-summarize: $1, skipping" >&2
  exit 0
}

[ -n "${PR_NUMBER:-}" ] || skip "not a pull request"
[ -d "$VERDICTS" ] || skip "no verdicts in this run"

WORK="$(mktemp -d "$RUNNER_TEMP/flake0-summarize-work.XXXXXX")"
MARKER="<!-- flake0-summary run=${GITHUB_RUN_ID:?} attempt=${GITHUB_RUN_ATTEMPT:?} -->"
ARGS=(--verdicts "$VERDICTS" --marker "$MARKER" --out "$WORK/body.md"
  --run-url "${GITHUB_SERVER_URL:-https://github.com}/$REPO/actions/runs/$GITHUB_RUN_ID/attempts/$GITHUB_RUN_ATTEMPT")

if [ "${MODE:-update}" = final ]; then
  ARGS+=(--final --model "${MODEL:-}" --diff "$WORK/diff.patch")
  # FLAKE0_CLASSIFY_NO_DIFF: see classify's run.sh
  if [ -n "${OPENROUTER_API_KEY:-}" ] && [ -z "${FLAKE0_CLASSIFY_NO_DIFF:-}" ]; then
    gh pr diff "$PR_NUMBER" -R "$REPO" > "$WORK/diff.patch" \
      || echo "flake0-summarize: could not fetch the diff of #$PR_NUMBER" >&2
  fi
  # Jobs whose classify left no verdict (setup failures, failed calls) have
  # finished by now, so their logs are served. Needs actions: read.
  if [ -n "${OPENROUTER_API_KEY:-}" ]; then
    ARGS+=(--logs "$WORK/logs")
    mkdir -p "$WORK/logs"
    find "$VERDICTS" -name verdict.json -exec jq -r 'select(type == "object" and has("error")) | .job_id | strings' {} + 2>/dev/null \
      | grep -xE '[0-9]+' | sort -u | while read -r id; do
        gh api --allow-escape-sequences "repos/$REPO/actions/jobs/$id/logs" > "$WORK/logs/$id.log" 2>/dev/null \
          || { rm -f "$WORK/logs/$id.log"; echo "flake0-summarize: could not fetch the log of job $id" >&2; }
      done
  fi
fi

python3 "${GITHUB_ACTION_PATH:?}/scripts/summarize.py" "${ARGS[@]}" || skip "summarize.py found nothing to post"

# Bot comments only: a person can paste the marker, and this token could delete
# their comment. startswith: model text further down the body can hold a marker.
IDS="$(gh api --paginate "repos/$REPO/issues/$PR_NUMBER/comments" \
  --jq ".[] | select(.user.type == \"Bot\" and (.body | startswith(\"$MARKER\"))) | .id")" \
  || skip "could not list the comments of #$PR_NUMBER"
FIRST="$(head -n1 <<< "$IDS")"

if [ -n "$FIRST" ]; then
  gh api -X PATCH "repos/$REPO/issues/comments/$FIRST" -F "body=@$WORK/body.md" > /dev/null \
    || echo "::warning::flake0-summarize could not edit comment $FIRST"
else
  gh api -X POST "repos/$REPO/issues/$PR_NUMBER/comments" -F "body=@$WORK/body.md" > /dev/null \
    || echo "::warning::flake0-summarize could not comment on #$PR_NUMBER"
fi
# Two jobs that both found no comment each posted one. Keep the oldest.
for id in $(tail -n +2 <<< "$IDS"); do
  gh api -X DELETE "repos/$REPO/issues/comments/$id" > /dev/null || true
done
exit 0
