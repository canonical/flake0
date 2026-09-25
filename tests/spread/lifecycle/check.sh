#!/usr/bin/env bash
# Checks the collect bundle after the lifecycle spread task.
set -euo pipefail

B="${1:?usage: check.sh <extracted-bundle-dir> <controller>}"
CTL="${2:?usage: check.sh <extracted-bundle-dir> <controller>}"
LOG="$B/juju-debug-root-$CTL"
STATUS="$B/juju-status-root-$CTL"
fail() { echo "FAIL: $*"; exit 1; }

for m in controller testing extra; do
  [ -s "$LOG-$m.log" ] || fail "missing $LOG-$m.log"
done
grep -q '^# flake0: reconnect' "$LOG-testing.log" || fail "no reconnect in testing"
# the task logged the token after dropping the stream
TOKEN=$(cat /tmp/flake0-sim/token)
awk -v t="$TOKEN" '/^# flake0: reconnect/ {r = 1} r && index($0, t) {ok = 1} END {exit !ok}' \
  "$LOG-testing.log" || fail "token missing after the reconnect in testing"
grep -q '^# flake0: ended' "$LOG-extra.log" || fail "no ended marker in extra"

for m in testing extra; do
  [ -s "$STATUS-$m.ndjson" ] || fail "missing $STATUS-$m.ndjson"
  jq -ne 'all(inputs; has("ts") and has("status"))' "$STATUS-$m.ndjson" > /dev/null || fail "bad $STATUS-$m.ndjson"
done
[ ! -e "$STATUS-controller.ndjson" ] || fail "the controller model got status"
if compgen -G "$B/juju-*-user-*" > /dev/null; then fail "the runner user's store should be empty"; fi
echo "bundle OK"
