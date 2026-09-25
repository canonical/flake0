#!/usr/bin/env bash
# flake0-collect: follow Juju model logs and status for the life of the job.
# start.sh launches it under setsid and stop-watcher.sh kills its process tree.
# It writes nothing until a Juju controller exists and never prints.
#
# It reads the runner user's client store, and root's when passwordless sudo
# works. Spread runs integration tests as root, so consumer controllers live in
# root's store. Each store is queried as its owner, because mixing users on one
# store fails on its lock file.
set -uo pipefail

COLLECT_DIR="${1:?usage: juju-watch.sh <collect-dir>}"
echo "$$" > "$COLLECT_DIR/watcher.pid"

# FLAKE0_WATCH_* exist for tests/test_juju_watch.py.
INTERVAL="${FLAKE0_WATCH_INTERVAL:-30}"
RETRY="${FLAKE0_WATCH_RETRY:-5}"
CAP="${FLAKE0_WATCH_CAP:-52428800}"  # 50 MB, then reconnects stop replaying
read -r -a SUDO <<< "${FLAKE0_WATCH_SUDO:-sudo -n}"
SNAP_JUJU="${FLAKE0_WATCH_SNAP_JUJU:-/snap/bin/juju}"
ROOT_STORE="${FLAKE0_WATCH_ROOT_STORE:-/root/.local/share/juju}"
USER_STORE="${JUJU_DATA:-$HOME/.local/share/juju}"
WLOG="$COLLECT_DIR/watcher.log"
declare -A FOLLOWER=() LOGGED=()

# bash's EPOCHREALTIME, because uutils `date` (Ubuntu 25.10+) ignores %3N's width
export TZ=UTC
ts() {
  local t=$EPOCHREALTIME frac
  frac=${t#*[.,]}
  printf '%(%Y-%m-%dT%H:%M:%S)T.%sZ' "${t%[.,]*}" "${frac:0:3}"
}

log() { printf '%s %s\n' "$(ts)" "$1" >> "$WLOG"; }

# Logs only when the message for a key changes, so a repeated failure costs one line.
log_once() {
  [ "${LOGGED[$1]:-}" = "$2" ] && return 0
  LOGGED[$1]="$2"
  [ -z "$2" ] || log "$2"
}

safe() { printf '%s' "${1//[^A-Za-z0-9_.-]/_}"; }

# Sets J to the command that runs juju as the owner of store $1.
use_store() {
  if [ "$1" = root ]; then J=("${SUDO[@]}" "$JUJU"); else J=("$JUJU"); fi
}

# One per model, in the background. Every reconnect replays the history, so
# markers bound the duplicates for phase 4 ingest. Without a terminal, juju
# 3.6 exits after the history unless given --tail. --retry would reconnect with
# no marker, so it is not used.
follow() {
  local store=$1 ctl=$2 model=$3 n=0 rc file
  local -a replay=(--replay)
  file="$COLLECT_DIR/juju-debug-$store-$(safe "$ctl")-$(safe "$model").log"
  use_store "$store"
  while :; do
    echo "# flake0: connect $store:$ctl:$model at $(ts)" >> "$file"
    "${J[@]}" debug-log "${replay[@]}" --tail --date --utc --ms -m "$ctl:$model" >> "$file" 2>> "$WLOG"
    rc=$?
    if ! timeout 25 "${J[@]}" show-model "$ctl:$model" > /dev/null 2>&1; then
      echo "# flake0: ended at $(ts) after exit $rc" >> "$file"
      return 0
    fi
    if [ ${#replay[@]} -gt 0 ] && [ "$(stat -c %s "$file")" -ge "$CAP" ]; then
      replay=()
      echo "# flake0: capped at $(ts)" >> "$file"
    fi
    n=$((n + 1))
    echo "# flake0: reconnect $n at $(ts) after exit $rc" >> "$file"
    log "follower $store:$ctl:$model dropped (exit $rc), reconnect $n"
    sleep "$RETRY"
  done
}

status() {
  local store=$1 ctl=$2 model=$3 out
  if ! out=$(timeout 25 "${J[@]}" status --format=json -m "$ctl:$model" 2> /dev/null) \
    || [[ "$out" != \{* ]]; then
    log_once "status:$store:$ctl:$model" "status failed for $store:$ctl:$model"
    return 0
  fi
  log_once "status:$store:$ctl:$model" ""
  printf '{"ts":"%s","status":%s}\n' "$(ts)" "${out//$'\n'/}" \
    >> "$COLLECT_DIR/juju-status-$store-$(safe "$ctl")-$(safe "$model").ndjson"
}

# Prints "<controller> TAB <model> TAB <1 if controller model>" for store $1.
models() {
  local ctl
  "${J[@]}" controllers --format=json 2> /dev/null \
    | python3 -c 'import json, sys; [print(c) for c in json.load(sys.stdin)["controllers"] or {}]' 2> /dev/null \
    | while read -r ctl; do
        timeout 25 "${J[@]}" models -c "$ctl" --format=json 2> /dev/null | python3 -c '
import json, sys
for m in json.load(sys.stdin)["models"]:
    print(sys.argv[1], m["short-name"], int(m.get("is-controller", False)), sep="\t")' "$ctl" 2> /dev/null
      done
}

tick() {
  local store ctl model is_ctl key
  JUJU=$(command -v juju) || { [ -x "$SNAP_JUJU" ] && JUJU=$SNAP_JUJU; } || return 0
  for store in user root; do
    if [ "$store" = user ]; then
      [ -s "$USER_STORE/controllers.yaml" ] || continue
    else
      # when the watcher runs as root, the user store already is root's
      [ "$(id -u)" != 0 ] || continue
      "${SUDO[@]}" test -s "$ROOT_STORE/controllers.yaml" 2> /dev/null || continue
    fi
    use_store "$store"
    log_once "store:$store" "found $store client store, using $JUJU"
    while IFS=$'\t' read -r ctl model is_ctl; do
      key="$store:$ctl:$model"
      if [ -z "${FOLLOWER[$key]:-}" ] || ! kill -0 "${FOLLOWER[$key]}" 2> /dev/null; then
        log "following $key"
        follow "$store" "$ctl" "$model" &
        FOLLOWER[$key]=$!
      fi
      [ "$is_ctl" = 1 ] || status "$store" "$ctl" "$model"
    done < <(models)
  done
}

while :; do
  tick
  sleep "$INTERVAL"
done
