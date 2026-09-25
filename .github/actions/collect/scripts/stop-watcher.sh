#!/usr/bin/env bash
# flake0-collect: stop the Juju log watcher and everything it started. stop.sh
# calls it before bundling. It never fails and never prints.
set -uo pipefail

DIR="${1:?usage: stop-watcher.sh <collect-dir>}"
PID="$(cat "$DIR/watcher.pid" 2> /dev/null)" || exit 0
rm -f "$DIR/watcher.pid"
# a stale pid may belong to another process by now
grep -qs juju-watch.sh "/proc/$PID/cmdline" || exit 0

# Root-store followers run under sudo, and only root can signal them.
SUDO=()
if sudo -n true 2> /dev/null; then SUDO=(sudo -n); fi

# Collect the tree first, because children are reparented once their parent dies.
tree() {
  local child
  for child in $(pgrep -P "$1"); do
    echo "$child"
    tree "$child"
  done
}
mapfile -t PIDS < <(echo "$PID"; tree "$PID")

"${SUDO[@]}" kill -TERM "${PIDS[@]}" 2> /dev/null
sleep 2
"${SUDO[@]}" kill -KILL "${PIDS[@]}" 2> /dev/null
exit 0
