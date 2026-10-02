#!/usr/bin/env bash
# flake0-collect: follow LXD container and Kubernetes pod logs for the life of
# the job. start.sh launches it as root under setsid, because every source is
# root-only, and stop-watcher.sh kills its process tree. It writes nothing until
# a container or pod exists and never prints.
set -uo pipefail

COLLECT_DIR="${1:?usage: workload-watch.sh <collect-dir>}"

# FLAKE0_WATCH_* exist for tests/test_workload_watch.py.
INTERVAL="${FLAKE0_WATCH_INTERVAL:-15}"
LXC="${FLAKE0_WATCH_LXC:-/snap/bin/lxc}"
LXD_DIR="${FLAKE0_WATCH_LXD_DIR:-/var/snap/lxd/common/lxd}"
CONTAINERS_DIR="${FLAKE0_WATCH_CONTAINERS_DIR:-/var/log/containers}"
PROC="${FLAKE0_WATCH_PROC:-/proc}"
WLOG="$COLLECT_DIR/workload-watcher.log"
declare -A FOLLOWER=()

echo "$$" > "$COLLECT_DIR/workload-watcher.pid"

# Same as juju-watch.sh, so both write one timestamp format.
export TZ=UTC
ts() {
  local t=$EPOCHREALTIME frac
  frac=${t#*[.,]}
  printf '%(%Y-%m-%dT%H:%M:%S)T.%sZ' "${t%[.,]*}" "${frac:0:3}"
}

safe() { printf '%s' "${1//[^A-Za-z0-9_.-]/_}"; }

# Starts a follower for source $1 unless one is running. It appends a connect
# marker, then the output of the remaining command, to file $2. Followers get no
# stdin, because lxc exec would read the discovery list being looped over.
follow() {
  local source=$1 file=$2
  shift 2
  [ -n "${FOLLOWER[$source]:-}" ] && kill -0 "${FOLLOWER[$source]}" 2> /dev/null && return 0
  mkdir -p "${file%/*}"
  echo "# flake0: connect $source at $(ts)" >> "$file"
  "$@" < /dev/null >> "$file" 2>> "$WLOG" &
  FOLLOWER[$source]=$!
}

# The kubelet links each container instance's stdout file here, so a restart
# gets a new link. uutils tail misses appends when -F follows a symlink, so the
# follower tails the link's target. The kubelet removes the link with its
# container, and tail -F never exits, so a crash-looping pod would add one tail
# per restart without the second loop.
follow_pods() {
  local link key
  for link in "$CONTAINERS_DIR"/*.log; do
    key="k8s:${link##*/}"
    [ -L "$link" ] && [ -z "${FOLLOWER[$key]:-}" ] || continue
    follow "$key" "$COLLECT_DIR/workload/k8s/${link##*/}" tail -n +1 -F "$(readlink "$link")"
  done
  for key in "${!FOLLOWER[@]}"; do
    if [[ "$key" == k8s:* ]] && [ ! -L "$CONTAINERS_DIR/${key#k8s:}" ]; then
      kill "${FOLLOWER[$key]}" 2> /dev/null
      unset "FOLLOWER[$key]"
    fi
  done
}

# LXD runs one "[lxc monitor] <lxd dir>/containers <name>" process per running
# container, and the container's init is its oldest child. Hooks the monitor
# runs while a container starts or stops are named lxd and run on the host, so
# they are skipped. Reading these processes instead of running `lxc list` never
# starts an idle LXD, and never runs the lxd-installer shim, which installs LXD
# and is /usr/sbin/lxc on Ubuntu 24.04 and later. A container in another project
# is named <project>_<name>. Followers are keyed on the init pid, so a restarted
# container gets new ones, and --pid ends the old file tails.
follow_containers() {
  local mpid dir name pid comm out f rel
  local -a instance children
  while read -r mpid _ _ dir name _; do
    [ "$dir" = "$LXD_DIR/containers" ] || continue
    # The children file has no trailing newline, so read fails after reading it.
    children=() pid=""
    read -r -a children 2> /dev/null < "/proc/$mpid/task/$mpid/children"
    for pid in "${children[@]}"; do
      read -r comm 2> /dev/null < "/proc/$pid/comm" && [ "$comm" != lxd ] && break
      pid=""
    done
    [ -n "$pid" ] || continue
    out="$COLLECT_DIR/workload/lxd/$(safe "$name")"
    instance=("$name")
    [[ "$name" == *_* ]] && instance=(--project "${name%%_*}" "${name#*_}")
    if [ -x "$LXC" ]; then
      follow "lxd:$name:$pid:journal" "$out/journal.log" \
        "$LXC" exec "${instance[@]}" -- journalctl --utc --no-tail -f -o short-iso-precise
    fi
    while IFS= read -r f; do
      rel=${f#"$PROC/$pid/root/var/snap/"} # <snap>/common/var/log/<dir>/<file>
      follow "lxd:$name:$pid:${f#"$PROC/$pid/root"}" \
        "$out/$(safe "${rel%%/*}/${rel#*/common/var/log/}")" \
        tail -n +1 -F --pid="$pid" "$f"
    done < <(find "$PROC/$pid/root/var/snap" -mindepth 6 -maxdepth 6 -type f \
      -path '*/common/var/log/*/*.log' 2> /dev/null)
  done < <(pgrep -a -f '^\[lxc monitor\] ')
}

while :; do
  follow_pods
  follow_containers
  sleep "$INTERVAL"
done
