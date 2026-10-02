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

W="$B/workload"
if [ "$CTL" = concierge-lxd ]; then
  grep -qF "$TOKEN" "$W"/lxd/*/journal.log || fail "token missing from the LXD journals"
  grep -q 'Ready to accept connections' "$W"/lxd/*/valkey-charmed_valkey_valkey.log \
    || fail "valkey's startup missing from its snap log"
  # valkey logs this when the task removes it, long after the follower started
  grep -q 'ready to exit' "$W"/lxd/*/valkey-charmed_valkey_valkey.log \
    || fail "valkey's shutdown missing from its snap log"
  # one for the controller's container and one for valkey's
  [ "$(compgen -G "$W/lxd/*/journal.log" | wc -l)" -ge 2 ] || fail "fewer than 2 LXD journals"
else
  grep -qF "$TOKEN" "$W"/k8s/valkey-0_testing_charm-[0-9a-f]*.log \
    || fail "token missing from the charm container's stdout"
  # pebble prefixes each line of valkey's log-tailing service with its name
  grep -qF '[valkey-logs]' "$W"/k8s/valkey-0_testing_valkey-*.log \
    || fail "valkey's log missing from the valkey container's stdout"
  compgen -G "$W/k8s/controller-0_controller-${CTL}_*.log" > /dev/null || fail "no controller pod logs"
fi
echo "bundle OK"
