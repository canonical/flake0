#!/usr/bin/env bash
# flake0-collect: stop the Juju and workload log watchers and everything they
# started. stop.sh calls it before bundling. It never fails and never prints.
set -uo pipefail

DIR="${1:?usage: stop-watcher.sh <collect-dir>}"

# Followers run under sudo, and only root can signal them.
SUDO=()
if sudo -n true 2> /dev/null; then SUDO=(sudo -n); fi

# Prints $1 and its descendants. It stops each process before listing its
# children, so no follower can start between this walk and the kill.
tree() {
  local child
  "${SUDO[@]}" kill -STOP "$1" 2> /dev/null
  echo "$1"
  for child in $(pgrep -P "$1"); do
    tree "$child"
  done
}

# Collect both trees first, because children are reparented once their parent dies.
PIDS=()
for watcher in watcher:juju-watch.sh workload-watcher:workload-watch.sh; do
  PID="$(cat "$DIR/${watcher%%:*}.pid" 2> /dev/null)" || continue
  rm -f "$DIR/${watcher%%:*}.pid"
  # a stale pid may belong to another process by now
  grep -qs "${watcher#*:}" "/proc/$PID/cmdline" || continue
  mapfile -t -O "${#PIDS[@]}" PIDS < <(tree "$PID")
done
[ ${#PIDS[@]} -gt 0 ] || exit 0

# A stopped process acts on TERM only once it continues.
"${SUDO[@]}" kill -TERM "${PIDS[@]}" 2> /dev/null
"${SUDO[@]}" kill -CONT "${PIDS[@]}" 2> /dev/null
sleep 2
"${SUDO[@]}" kill -KILL "${PIDS[@]}" 2> /dev/null
exit 0
