#!/usr/bin/env bash
# flake0-collect: follow the kernel log and snapd's journal for the life of the
# job. start.sh launches it as root under setsid, because Ubuntu restricts dmesg,
# and stop-watcher.sh kills its process tree. Both followers print only new lines:
# env-start.json already lists what snapd did before the job.
set -uo pipefail

COLLECT_DIR="${1:?usage: host-watch.sh <collect-dir>}"

echo "$$" > "$COLLECT_DIR/host-watcher.pid"

dmesg -W --time-format iso < /dev/null >> "$COLLECT_DIR/kernel.log" 2>> "$COLLECT_DIR/host-watcher.log" &
journalctl -u snapd -f -n 0 --utc -o short-iso-precise < /dev/null \
  >> "$COLLECT_DIR/snapd.log" 2>> "$COLLECT_DIR/host-watcher.log" &
wait
