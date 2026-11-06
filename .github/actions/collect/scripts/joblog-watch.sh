#!/usr/bin/env bash
# flake0-collect: keep the runner's own log of this job. The runner writes it to
# _diag/pages/<planId>_<jobId>_<n>.log in ~8 MB pages and deletes each page once
# uploaded, so this hard-links every page of the job into the collect dir as it
# appears. stop.sh joins them into job.log. These are undocumented runner
# internals: if they change, job.log is missing and the job carries on.
# start.sh launches it under setsid with FLAKE0_RUNNER_DIAG set, and
# stop-watcher.sh stops it.
set -uo pipefail

DIR="${1:?usage: joblog-watch.sh <collect-dir>}"
DIAG="${FLAKE0_RUNNER_DIAG:?}"
PAGES="$DIR/joblog-pages"
JOB_ID=""

echo "$$" > "$DIR/joblog-watcher.pid"
mkdir -p "$PAGES"

keep() {
  local f
  # The newest Worker log is this job's, because a runner runs one job at a time.
  [ -n "$JOB_ID" ] || JOB_ID="$(cat "$DIAG"/Worker_*.log 2> /dev/null \
    | sed -n 's/.*JobRunner\] Job ID \([0-9a-f-]*\).*/\1/p' | tail -1)"
  [ -n "$JOB_ID" ] || return 0
  for f in "$DIAG/pages/"*_"$JOB_ID"_*.log; do
    [ -e "$f" ] || continue
    # shortcut: across filesystems cp can miss the last 0.5 s of a page before
    # its upload. Move the pages dir next to _diag if a runner needs that.
    [ "$f" -ef "$PAGES/${f##*/}" ] || ln -f "$f" "$PAGES/" 2> /dev/null || cp -u "$f" "$PAGES/"
  done
}

# A last pass at stop catches a page opened since the previous one.
trap 'keep; exit 0' TERM
while :; do
  keep
  sleep 0.5
done
