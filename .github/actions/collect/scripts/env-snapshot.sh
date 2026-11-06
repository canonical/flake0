#!/usr/bin/env bash
# flake0-collect: write what the machine looks like to <out-file>, so env-start.json
# and env-end.json show what snapd changed. Snaps and changes come from snapd's
# REST API, which needs no root. They are null without snapd. It never fails.
# Not redacted, because redact.sed can empty a one-line JSON file. The README warns
# that a change's err and log fields can hold snap hook output.
set -uo pipefail

OUT="${1:?usage: env-snapshot.sh <out-file>}"
# FLAKE0_SNAPD_SOCKET exists for tests/test_env_snapshot.py.
SOCK="${FLAKE0_SNAPD_SOCKET:-/run/snapd.socket}"

# Same as juju-watch.sh, so every file has one timestamp format.
export TZ=UTC
ts() {
  local t=$EPOCHREALTIME frac
  frac=${t#*[.,]}
  printf '%(%Y-%m-%dT%H:%M:%S)T.%sZ' "${t%[.,]*}" "${frac:0:3}"
}

get() {
  curl -sf --max-time 10 --unix-socket "$SOCK" "http://localhost$1" 2> /dev/null \
    | jq -c '.result' 2> /dev/null || echo null
}

# On stdin, because the changes list can pass the 128 KB limit on one argument.
{
  get /v2/snaps
  get '/v2/changes?select=all'
} | jq -cs --arg ts "$(ts)" --arg kernel "$(uname -r)" \
  '{ts: $ts, kernel: $kernel, snaps: .[0], changes: .[1]}' > "$OUT" 2> /dev/null
exit 0
