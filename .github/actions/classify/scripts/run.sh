#!/usr/bin/env bash
# flake0-classify: an opencode agent investigates why this job failed, from its
# collect bundle and source, and writes verdict.json.
# Runs as the last step of the failed job, after PR code, so everything on
# disk and in the environment is untrusted. Must never fail the job.
# Hence no `set -e`.
set -uo pipefail
# $GITHUB_PATH lets PR code put its own tar, jq or git first
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

ACTION_PATH="${GITHUB_ACTION_PATH:-$(cd "$(dirname "$0")/.." && pwd)}"
FLAKE0_ROOT="$(cd "$ACTION_PATH/../../.." && pwd)"
OUTDIR="$(mktemp -d "${RUNNER_TEMP:-/tmp}/flake0-classify.XXXXXX")"
REPO="${GITHUB_REPOSITORY:-}"
RUN_URL="${GITHUB_SERVER_URL:-https://github.com}/$REPO/actions/runs/${GITHUB_RUN_ID:-}/attempts/${GITHUB_RUN_ATTEMPT:-1}"
# With the follow-up, 2 minutes under the 25 the README gives the step
TIMEOUT="${FLAKE0_CLASSIFY_TIMEOUT:-20m}"
FOLLOW_UP_TIMEOUT=3m
# The steps cap in opencode.json, sized to end before the timeout: a capped
# agent gets one last turn to answer, a timed out one leaves nothing. Steps
# took ~22 s on a real job, some over 3 minutes.
STEPS="$(jq -r '.agent["flaky-test-investigator"].steps' "$ACTION_PATH/opencode.json")"

emit_output() {
  [ -n "${GITHUB_OUTPUT:-}" ] && echo "$1=$2" >> "$GITHUB_OUTPUT"
  return 0
}

[ -n "${OPENROUTER_API_KEY:-}" ] || {
  echo "flake0-classify: no OpenRouter API key (fork pull requests get no secrets), skipping" >&2
  emit_output verdict-path ""
  exit 0
}

WORK="$(mktemp -d "${RUNNER_TEMP:-/tmp}/flake0-classify-work.XXXXXX")"
CFG="$(mktemp -d "${RUNNER_TEMP:-/tmp}/flake0-classify-cfg.XXXXXX")"
OCHOME="$(mktemp -d "${RUNNER_TEMP:-/tmp}/flake0-classify-home.XXXXXX")"
mkdir -p "$WORK/bundle" "$WORK/src" "$WORK/docs" "$CFG/agents" "$CFG/skills"
cp "$FLAKE0_ROOT/.opencode/agents/flaky-test-investigator.md" "$CFG/agents/"
cp -r "$FLAKE0_ROOT/skills/integration-test-diagnosis" "$CFG/skills/"
cp "$FLAKE0_ROOT/docs/taxonomy.md" "$WORK/docs/"

if [ -n "${BUNDLE_PATH:-}" ] && [ -f "$BUNDLE_PATH" ]; then
  tar -xf "$BUNDLE_PATH" -C "$WORK/bundle" 2> /dev/null \
    || echo "flake0-classify: could not extract the bundle" >&2
else
  echo "flake0-classify: no collect bundle at '${BUNDLE_PATH:-}'" >&2
fi
# Git runs filters and hooks from the workspace's .git/config, which PR code
# controls, so it gets no secrets.
if [ -e "${GITHUB_WORKSPACE:-}/.git" ]; then
  env -i PATH="$PATH" HOME="$OCHOME" git -C "$GITHUB_WORKSPACE" archive --format=tar HEAD \
    | tar -xf - -C "$WORK/src" 2> /dev/null \
    || echo "flake0-classify: could not export the source" >&2
fi
# opencode's read tool follows symlinks out of the directory, and a FIFO hangs it
find "$WORK" ! -type f ! -type d -delete
# read and grep cut lines at ~2000 characters, and Telegraf writes a whole
# batch per line. One metric per line keeps every value.
if [ -f "$WORK/bundle/metrics.json" ]; then
  jq -c '.metrics[]?' "$WORK/bundle/metrics.json" > "$WORK/bundle/metrics.ndjson" 2> /dev/null \
    && rm "$WORK/bundle/metrics.json"
fi

PROMPT="Job \"${JOB_NAME:-}\" of $RUN_URL failed, at commit ${GITHUB_SHA:-unknown}"
[ -n "${PR_NUMBER:-}" ] && PROMPT+=" of pull request #$PR_NUMBER"
PROMPT+=" in $REPO. The job is still running, so GitHub doesn't serve its log yet.

The working directory holds:
- bundle/: the job's collect bundle. job.log is the job's own log up to this step, pytest.log
  the test output, and metrics.ndjson one Telegraf metric per line, every 2 seconds.
- src/: the repository at the tested commit.
- docs/taxonomy.md: the flake0 taxonomy.

Files in bundle/, with sizes in bytes:
$(cd "$WORK" && find bundle -type f -printf '%s %p\n' | sort -k2)

Use gh with --repo $REPO for run history and the pull request diff, one bare command at a time:
no pipes, no 2>&1, no --jq. For the jobs of a run, use
gh api \"repos/$REPO/actions/runs/${GITHUB_RUN_ID:-}/attempts/${GITHUB_RUN_ATTEMPT:-1}/jobs?per_page=30\":
gh run view fails on runs with many jobs.

You have $STEPS steps, each a turn of tool calls. Check the pull request diff and the run history
early, before reading source in depth: they decide between a regression, a pre-existing bug and
flakiness. Write the verdict before you run out of steps.

When done, write verdict.json in the working directory, with only this JSON object:
{\"verdict\": \"REAL_FAILURE\" | \"FLAKY\" | \"INCONCLUSIVE\",
 \"root_cause\": \"<root cause category>\", \"recommendation\": \"<recommendation>\",
 \"confidence_pct\": <0-100>, \"confidence_bucket\": \"high\" | \"medium\" | \"low\",
 \"failed_tests\": [\"<failed test function>\", ...],
 \"summary\": \"<one sentence for a pull request comment>\",
 \"suggested_fix\": \"<one sentence, or empty>\", \"error_excerpt\": \"<the key error lines>\",
 \"reasoning\": \"<a short paragraph>\", \"evidence\": [\"<one signal and where it is>\", ...]}"

# env -i: PR code can set OPENCODE_* and XDG_* through $GITHUB_ENV.
# OPENCODE_DISABLE_PROJECT_CONFIG: opencode.json, .opencode/ and AGENTS.md in
# parent directories would otherwise load, and PR code can write them.
ask() { # <timeout> <opencode run arguments...>
  local limit="$1"
  shift
  (cd "$WORK" && env -i PATH="$PATH" LANG=C.UTF-8 HOME="$OCHOME" \
    XDG_CONFIG_HOME="$OCHOME/.config" XDG_DATA_HOME="$OCHOME/.local/share" \
    XDG_CACHE_HOME="$OCHOME/.cache" XDG_STATE_HOME="$OCHOME/.local/state" \
    OPENCODE_CONFIG="$ACTION_PATH/opencode.json" OPENCODE_CONFIG_DIR="$CFG" \
    OPENCODE_DISABLE_PROJECT_CONFIG=1 \
    OPENROUTER_API_KEY="$OPENROUTER_API_KEY" GH_TOKEN="${GH_TOKEN:-}" \
    timeout "$limit" "${OPENCODE_BIN:?}" run --standalone --agent flaky-test-investigator \
    --model "openrouter/${MODEL:-deepseek/deepseek-v4.1-flash}" --format json "$@" \
    >> "$OUTDIR/session.ndjson" 2>> "$OUTDIR/opencode.log")
}

# At the steps cap the agent answers in text, so take the verdict from there
have_verdict() {
  [ -e "$WORK/verdict.json" ] && return 0
  if python3 -I "$ACTION_PATH/scripts/final_verdict.py" "$OUTDIR/session.ndjson" > "$WORK/verdict.json" 2> /dev/null; then
    echo "flake0-classify: no verdict.json, so the verdict comes from the agent's last message" >&2
    return 0
  fi
  rm -f "$WORK/verdict.json"
  return 1
}

RC=0
# --title: without one, opencode sends the prompt to a second model to name the session.
ask "$TIMEOUT" --title "flake0 classify" "$PROMPT" || RC=$?
# A provider error, the timeout or the steps cap can end the run before the
# verdict. One more message in the same session asks for it. It also ends the
# request on a user turn, which Gemini needs after the cap.
if ! have_verdict; then
  SESSION="$(jq -r '.sessionID // empty' "$OUTDIR/session.ndjson" 2> /dev/null | head -n 1)"
  if [ -n "$SESSION" ]; then
    echo "flake0-classify: no verdict, asking the agent for it once more" >&2
    ask "$FOLLOW_UP_TIMEOUT" --session "$SESSION" "Stop investigating. Write verdict.json now, \
from what you found so far, in the JSON shape given at the start. Use INCONCLUSIVE if the \
evidence is not enough."
    have_verdict
  fi
fi

ERROR=""
if [ ! -f "$WORK/verdict.json" ] || [ -L "$WORK/verdict.json" ]; then
  if [ "$RC" -eq 124 ]; then
    ERROR="opencode timed out after $TIMEOUT"
  else
    ERROR="opencode exited $RC without writing a verdict"
  fi
  # opencode reports errors (unknown model, provider down) as events, not on stderr
  LAST="$(jq -r 'select(.type == "error") | .error.message // empty' "$OUTDIR/session.ndjson" 2> /dev/null | tail -n 1)"
  [ -n "$LAST" ] && ERROR+=": ${LAST:0:300}"
elif ! jq -e 'type == "object"' "$WORK/verdict.json" > /dev/null 2>&1; then
  ERROR="the verdict is not a JSON object"
fi
if [ -z "$ERROR" ]; then
  jq --arg job "${JOB_NAME:-}" --arg model "${MODEL:-}" '. + {job: $job, model: $model}' \
    "$WORK/verdict.json" > "$OUTDIR/verdict.json"
else
  echo "flake0-classify: $ERROR" >&2
  tail -n 20 "$OUTDIR/opencode.log" >&2
  jq -n --arg job "${JOB_NAME:-}" --arg model "${MODEL:-}" --arg error "$ERROR" \
    '{job: $job, model: $model, verdict: "INCONCLUSIVE", error: $error}' > "$OUTDIR/verdict.json"
fi

# The API serves job logs but not job summaries
cat "$OUTDIR/verdict.json"
echo
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  python3 -I "$FLAKE0_ROOT/.github/actions/summarize/scripts/summarize.py" \
    --job-summary "$OUTDIR/verdict.json" >> "$GITHUB_STEP_SUMMARY" \
    || echo "flake0-classify: could not write the job summary" >&2
fi
emit_output verdict-path "$OUTDIR/verdict.json"
exit 0
