#!/usr/bin/env bash
# flake0-collect: follow LXD container and Kubernetes pod logs for the life of
# the job. start.sh launches it under setsid and stop-watcher.sh kills its
# process tree. It writes nothing until a container or pod exists and never
# prints. See docs/phase2-workload-logs.md.
#
# Every source is root-only, so without root or passwordless sudo it exits.
set -uo pipefail

COLLECT_DIR="${1:?usage: workload-watch.sh <collect-dir>}"

# FLAKE0_WATCH_* exist for tests/test_workload_watch.py.
INTERVAL="${FLAKE0_WATCH_INTERVAL:-15}"
read -r -a SUDO <<< "${FLAKE0_WATCH_SUDO:-sudo -n}"
LXC="${FLAKE0_WATCH_LXC:-/snap/bin/lxc}"
LXD_DIR="${FLAKE0_WATCH_LXD_DIR:-/var/snap/lxd/common/lxd}"
CONTAINERS_DIR="${FLAKE0_WATCH_CONTAINERS_DIR:-/var/log/containers}"
PROC="${FLAKE0_WATCH_PROC:-/proc}"
WLOG="$COLLECT_DIR/workload-watcher.log"
declare -A FOLLOWER=()

if [ "$(id -u)" = 0 ]; then
  SUDO=()
elif ! "${SUDO[@]}" true 2> /dev/null; then
  exit 0
fi
echo "$$" > "$COLLECT_DIR/workload-watcher.pid"

# Same as juju-watch.sh, so both write one timestamp format.
export TZ=UTC
ts() {
  local t=$EPOCHREALTIME frac
  frac=${t#*[.,]}
  printf '%(%Y-%m-%dT%H:%M:%S)T.%sZ' "${t%[.,]*}" "${frac:0:3}"
}

safe() { printf '%s' "${1//[^A-Za-z0-9_.-]/_}"; }

# Starts a follower under key $1 unless one is running. It appends a connect
# marker for source $2, then the output of the remaining command, to file $3.
# Followers run as root through sudo, and kill -0 fails on them for this user,
# so liveness comes from /proc.
follow() {
  local key=$1 source=$2 file=$3
  shift 3
  [ -n "${FOLLOWER[$key]:-}" ] && [ -e "/proc/${FOLLOWER[$key]}" ] && return 0
  mkdir -p "$(dirname "$file")"
  echo "# flake0: connect $source at $(ts)" >> "$file"
  "$@" >> "$file" 2>> "$WLOG" &
  FOLLOWER[$key]=$!
}

# The kubelet links each container instance's stdout file here, so a restart
# gets a new link. uutils tail misses appends when -F follows a symlink, so the
# follower tails the link's target.
follow_pods() {
  local link target
  for link in "$CONTAINERS_DIR"/*.log; do
    target=$(readlink "$link") || continue
    follow "k8s:$link" "k8s:${link##*/}" "$COLLECT_DIR/workload/k8s/${link##*/}" \
      "${SUDO[@]}" tail -n +1 -F "$target"
  done
}

# LXD runs one "[lxc monitor] <lxd dir>/containers <name>" process per running
# container, and the container's init is its child. Reading these processes
# instead of running `lxc list` never starts an idle LXD, and never runs the
# lxd-installer shim, which installs LXD and is /usr/sbin/lxc on Ubuntu 24.04
# and later. A container in another project is named <project>_<name>.
# Followers are keyed on the init pid, so a restarted container gets new ones,
# and --pid ends the old file tails.
follow_containers() {
  local mpid dir name pid out f rel
  local -a instance
  while read -r mpid _ _ dir name _; do
    [ "$dir" = "$LXD_DIR/containers" ] || continue
    pid=$(pgrep -P "$mpid" | head -1)
    [ -n "$pid" ] || continue
    out="$COLLECT_DIR/workload/lxd/$(safe "$name")"
    instance=("$name")
    [[ "$name" == *_* ]] && instance=(--project "${name%%_*}" "${name#*_}")
    if [ -x "$LXC" ]; then
      follow "lxd:$name:$pid:journal" "lxd:$name:$pid:journal" "$out/journal.log" \
        "${SUDO[@]}" "$LXC" exec "${instance[@]}" -- journalctl --utc --no-tail -f -o short-iso-precise
    fi
    while IFS= read -r f; do
      rel=${f#"$PROC/$pid/root/var/snap/"} # <snap>/common/var/log/<dir>/<file>
      follow "lxd:$name:$pid:$f" "lxd:$name:$pid:${f#"$PROC/$pid/root"}" \
        "$out/$(safe "${rel%%/*}/${rel#*/common/var/log/}")" \
        "${SUDO[@]}" tail -n +1 -F --pid="$pid" "$f"
    done < <("${SUDO[@]}" find "$PROC/$pid/root/var/snap" -mindepth 6 -maxdepth 6 -type f \
      -path '*/common/var/log/*/*.log' 2> /dev/null)
  done < <(pgrep -a -f '^\[lxc monitor\] ')
}

while :; do
  follow_pods
  follow_containers
  sleep "$INTERVAL"
done
