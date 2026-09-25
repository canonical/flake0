#!/usr/bin/env bash
# flake0-collect — stop collection, finalize the bundle.
# Must be safe to run under `if: always()`: never fails the job, even if start
# never ran or the collector died mid-run. Hence no `set -e`.
set -uo pipefail

DIR_POINTER="${RUNNER_TEMP:-/tmp}/.flake0-collect-dir"
if [ -z "${FLAKE0_COLLECT_DIR:-}" ] && [ -s "$DIR_POINTER" ]; then
  FLAKE0_COLLECT_DIR="$(cat "$DIR_POINTER")"
fi
FLAKE0_COLLECT_DIR="${FLAKE0_COLLECT_DIR:-${RUNNER_TEMP:-/tmp}/flake0-collect}"
BUNDLE_PATH="${BUNDLE_PATH:-$(dirname "$FLAKE0_COLLECT_DIR")/flake0-bundle.tar.zst}"
PIDFILE="$FLAKE0_COLLECT_DIR/telegraf.pid"
LAUNCHER_PIDFILE="$FLAKE0_COLLECT_DIR/launcher.pid"
RESOLVED_CONF="$FLAKE0_COLLECT_DIR/telegraf.resolved.conf"

# Matches the collector but not the `sudo -n env ... telegraf --config ...`
# wrapper, whose command line contains the same config path but starts with
# sudo. Anchoring on the executable path is what separates them.
TELEGRAF_PATTERN="^[^ ]*telegraf --config ${RESOLVED_CONF}"

# Publish the path we actually wrote so the upload step never guesses. Set before
# any early exit so a missing bundle is a warning downstream, not a broken ref.
emit_output() {
  [ -n "${GITHUB_OUTPUT:-}" ] && echo "bundle-path=$1" >> "$GITHUB_OUTPUT"
  return 0
}

SUDO=()
if sudo -n true 2>/dev/null; then
  SUDO=(sudo -n)
fi

if [ ! -d "$FLAKE0_COLLECT_DIR" ]; then
  echo "flake0-collect: nothing to stop ($FLAKE0_COLLECT_DIR does not exist)"
  emit_output ""
  exit 0
fi

# --- stop telegraf: SIGTERM triggers a final flush + clean shutdown -----------
# $PIDFILE is written by telegraf itself (--pidfile), so this is the collector's
# pid, not the pid of the sudo wrapper that launched it.
#
# Reading it needs care: telegraf writes it 0640, and under sudo that is
# root-owned, so a plain `cat` fails for the runner user. start.sh
# relaxes the mode, but a collector started by an older version will not have
# been relaxed, hence the sudo read and the pgrep fallback.
PID="$(cat "$PIDFILE" 2>/dev/null || true)"
if [ -z "$PID" ] && [ ${#SUDO[@]} -gt 0 ]; then
  PID="$("${SUDO[@]}" cat "$PIDFILE" 2>/dev/null || true)"
fi
if [ -z "$PID" ]; then
  PID="$(pgrep -f "$TELEGRAF_PATTERN" 2>/dev/null | head -1 || true)"
  [ -n "$PID" ] && echo "flake0-collect: pidfile unreadable, matched collector by config path (pid $PID)"
fi

if [ -n "$PID" ] && [[ "$PID" =~ ^[0-9]+$ ]] && "${SUDO[@]}" kill -0 "$PID" 2>/dev/null; then
  echo "flake0-collect: stopping telegraf (pid $PID)"
  "${SUDO[@]}" kill -TERM "$PID" 2>/dev/null || true
  for _ in $(seq 1 15); do
    "${SUDO[@]}" kill -0 "$PID" 2>/dev/null || break
    sleep 1
  done
  if "${SUDO[@]}" kill -0 "$PID" 2>/dev/null; then
    echo "flake0-collect: SIGTERM timed out, sending SIGKILL (final flush lost)"
    "${SUDO[@]}" kill -KILL "$PID" 2>/dev/null || true
  fi
fi

# Safety net: never leave a root-owned collector running past the job, whatever
# happened above. Also stops it writing into the directory while we archive it.
if pgrep -f "$TELEGRAF_PATTERN" >/dev/null 2>&1; then
  echo "flake0-collect: collector still running, terminating by config path"
  "${SUDO[@]}" pkill -TERM -f "$TELEGRAF_PATTERN" 2>/dev/null || true
  for _ in $(seq 1 10); do
    pgrep -f "$TELEGRAF_PATTERN" >/dev/null 2>&1 || break
    sleep 1
  done
  "${SUDO[@]}" pkill -KILL -f "$TELEGRAF_PATTERN" 2>/dev/null || true
fi

# Wait for the launcher (sudo) to reap, so nothing is still writing into the
# directory while we archive it.
if [ -s "$LAUNCHER_PIDFILE" ]; then
  LAUNCHER_PID="$(cat "$LAUNCHER_PIDFILE")"
  if [[ "$LAUNCHER_PID" =~ ^[0-9]+$ ]]; then
    for _ in $(seq 1 10); do
      kill -0 "$LAUNCHER_PID" 2>/dev/null || break
      sleep 1
    done
    if kill -0 "$LAUNCHER_PID" 2>/dev/null; then
      kill -TERM "$LAUNCHER_PID" 2>/dev/null || true
    fi
  fi
fi
rm -f "$PIDFILE" "$LAUNCHER_PIDFILE" "$DIR_POINTER"

# --- stop the juju log watcher ---------------------------------------------------
# Before the chown and tar below, so nothing is still writing into the directory.
"$(dirname "${BASH_SOURCE[0]}")/stop-watcher.sh" "$FLAKE0_COLLECT_DIR"

# telegraf may have run as root — hand files back so artifact upload can read them
if [ ${#SUDO[@]} -gt 0 ]; then
  "${SUDO[@]}" chown -R -h "$(id -u):$(id -g)" "$FLAKE0_COLLECT_DIR" 2>/dev/null || true
fi

# --- bundle --------------------------------------------------------------------
# Name the file after what it actually contains: the previous version wrote gzip
# into a .tar.zst path whenever zstd was missing.
if ! command -v zstd >/dev/null 2>&1; then
  BUNDLE_PATH="${BUNDLE_PATH%.tar.zst}.tar.gz"
  echo "flake0-collect: zstd unavailable, falling back to gzip ($BUNDLE_PATH)"
  COMPRESS=(-z)
else
  COMPRESS=(--zstd)
fi

rm -f "$BUNDLE_PATH"
tar "${COMPRESS[@]}" -cf "$BUNDLE_PATH" -C "$FLAKE0_COLLECT_DIR" . || true

# tar creates the output file even when it fails part-way (a metric file still
# being written yields "file changed as we read it"), so existence proves
# nothing — read the archive back instead.
if tar -tf "$BUNDLE_PATH" >/dev/null 2>&1; then
  echo "flake0-collect: bundle at $BUNDLE_PATH ($(du -h "$BUNDLE_PATH" | cut -f1))"
  emit_output "$BUNDLE_PATH"
else
  echo "flake0-collect: WARNING: bundle is missing or unreadable, not uploading" >&2
  emit_output ""
fi
