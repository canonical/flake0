#!/usr/bin/env bash
# flake0-collect — start host metrics collection
# Downloads pinned Telegraf, launches it in the background.
# Idempotent: refuses to start twice over the same collection dir.
set -euo pipefail

# Hashes committed in-repo for the default version: a real supply-chain pin, so a
# tampered upstream tarball fails here. A caller-overridden version can only be
# checked against upstream's own published digest (corruption, not tampering) —
# see below.
PINNED_VERSION="1.39.2"

TELEGRAF_VERSION="${TELEGRAF_VERSION:-$PINNED_VERSION}"
ACTION_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FLAKE0_COLLECT_DIR="${FLAKE0_COLLECT_DIR:-${RUNNER_TEMP:-/tmp}/flake0-collect}"
TELEGRAF_HOME="${TELEGRAF_HOME:-${RUNNER_TEMP:-/tmp}/flake0-telegraf}"
PIDFILE="$FLAKE0_COLLECT_DIR/telegraf.pid"
LAUNCHER_PIDFILE="$FLAKE0_COLLECT_DIR/launcher.pid"
EXTRA_CONFIG="${FLAKE0_EXTRA_CONFIG:-}"

# --- tag values: default to "local" when not on a GitHub runner ---------------
# These are passed to Telegraf through `env` (below), which expands ${VARS} in
# telegraf.conf itself. They are never substituted into the config text: values
# may contain characters that are special to a templating tool but perfectly
# legal here (a workflow named "build | test" broke an earlier sed-based version).
: "${GITHUB_RUN_ID:=local}"
: "${GITHUB_RUN_ATTEMPT:=0}"
: "${GITHUB_REPOSITORY:=local}"
: "${GITHUB_WORKFLOW:=local}"
: "${GITHUB_JOB:=local}"
: "${GITHUB_SHA:=unknown}"
: "${GITHUB_REF_NAME:=unknown}"
: "${RUNNER_NAME:=$(hostname)}"
: "${RUNNER_OS:=$(uname -s)}"
: "${ImageOS:=unknown}"

TELEGRAF_ENV=(
  "FLAKE0_COLLECT_DIR=${FLAKE0_COLLECT_DIR}"
)
for var in GITHUB_RUN_ID GITHUB_RUN_ATTEMPT GITHUB_REPOSITORY \
           GITHUB_WORKFLOW GITHUB_JOB GITHUB_SHA GITHUB_REF_NAME \
           RUNNER_NAME RUNNER_OS ImageOS; do
  TELEGRAF_ENV+=("${var}=${!var}")
done

# Telegraf needs root for some /proc reads; sudo scrubs the environment, hence
# the explicit `env` prefix on launch rather than exported variables.
SUDO=()
if sudo -n true 2>/dev/null; then
  SUDO=(sudo -n)
fi

# --- sanity -------------------------------------------------------------------
PID="$(cat "$PIDFILE" 2>/dev/null || true)"
if [ -z "$PID" ] && [ ${#SUDO[@]} -gt 0 ]; then
  PID="$("${SUDO[@]}" cat "$PIDFILE" 2>/dev/null || true)"
fi
if [ -n "$PID" ] && [[ "$PID" =~ ^[0-9]+$ ]] && "${SUDO[@]}" kill -0 "$PID" 2>/dev/null; then
  echo "flake0-collect: already running (pid $PID)" >&2
  exit 1
fi
mkdir -p "$FLAKE0_COLLECT_DIR"
echo "$FLAKE0_COLLECT_DIR" > "${RUNNER_TEMP:-/tmp}/.flake0-collect-dir"

# --- install pinned Telegraf (static binary) ----------------------------------
case "$(uname -m)" in
  x86_64)  ARCH="amd64"; PINNED_SHA256="3ecf733bec389b8a0e1072f134ce379d79efe0d3caf984c164bd4cfc515a86d6" ;;
  aarch64) ARCH="arm64"; PINNED_SHA256="7626df978e86b4788aed477f7acb4528ff517b506c721f1bd4c9ac77464a93e5" ;;
  *) echo "flake0-collect: unsupported arch $(uname -m) (linux x86_64/aarch64 only)" >&2; exit 1 ;;
esac

TARBALL_NAME="telegraf-${TELEGRAF_VERSION}_linux_${ARCH}.tar.gz"
BASE_URL="https://dl.influxdata.com/telegraf/releases"
TELEGRAF_BIN="$TELEGRAF_HOME/telegraf-${TELEGRAF_VERSION}/usr/bin/telegraf"

if [ ! -x "$TELEGRAF_BIN" ]; then
  if [ "$TELEGRAF_VERSION" = "$PINNED_VERSION" ]; then
    EXPECTED_SHA256="$PINNED_SHA256"
  else
    # Overridden version: no in-repo hash exists for it, so verify against the
    # digest upstream publishes next to the tarball. This catches truncated or
    # corrupted downloads but, coming from the same origin, is not a defence
    # against a compromised origin. Prefer the pinned default.
    echo "flake0-collect: warning: telegraf-version '${TELEGRAF_VERSION}' is not the in-repo pinned ${PINNED_VERSION};" >&2
    echo "flake0-collect: warning: falling back to upstream-published digest (integrity check only)" >&2
    EXPECTED_SHA256="$(curl -fsSL --retry 3 --retry-delay 5 "${BASE_URL}/${TARBALL_NAME}.DIGESTS" | awk 'NR==1{print $1}')"
    if ! [[ "$EXPECTED_SHA256" =~ ^[0-9a-f]{64}$ ]]; then
      echo "flake0-collect: could not fetch a valid sha256 for ${TARBALL_NAME}" >&2
      exit 1
    fi
  fi

  echo "flake0-collect: installing telegraf v${TELEGRAF_VERSION} (${ARCH})"
  mkdir -p "$TELEGRAF_HOME"
  TMP_TARBALL="$(mktemp)"
  trap 'rm -f "$TMP_TARBALL"' EXIT
  curl -fsSL --retry 3 --retry-delay 5 "${BASE_URL}/${TARBALL_NAME}" -o "$TMP_TARBALL"
  echo "${EXPECTED_SHA256}  ${TMP_TARBALL}" | sha256sum -c - >/dev/null
  # tarball extracts to telegraf-<version>/ — the final layout, no move needed
  tar -xzf "$TMP_TARBALL" -C "$TELEGRAF_HOME"
  rm -f "$TMP_TARBALL"
  trap - EXIT
fi
echo "flake0-collect: using $("$TELEGRAF_BIN" version)"

# --- config -------------------------------------------------------------------
# Copied rather than used in place only so the conditional conntrack block can be
# appended; ${VARS} inside are left for Telegraf to expand.
RESOLVED_CONF="$FLAKE0_COLLECT_DIR/telegraf.resolved.conf"
cp "$ACTION_DIR/telegraf.conf" "$RESOLVED_CONF"

# conntrack table usage vs max, only where the conntrack module is loaded
if [ -r /proc/sys/net/netfilter/nf_conntrack_count ]; then
  cat >> "$RESOLVED_CONF" <<'EOF'

# Conntrack table usage vs max (appended: conntrack module present)
[[inputs.conntrack]]
EOF
fi

# Caller-supplied additions, e.g. an extra [[inputs.procstat]] block for a
# workload this action does not know about. Appended verbatim; the --test gate
# below rejects the whole config if it is malformed.
if [ -n "${EXTRA_CONFIG:-}" ]; then
  if [ ! -r "$EXTRA_CONFIG" ]; then
    echo "flake0-collect: extra-config not readable: $EXTRA_CONFIG" >&2
    exit 1
  fi
  echo "flake0-collect: appending extra config from $EXTRA_CONFIG"
  printf '\n# --- appended from %s ---\n' "$EXTRA_CONFIG" >> "$RESOLVED_CONF"
  cat "$EXTRA_CONFIG" >> "$RESOLVED_CONF"
fi

# --- validate then launch ------------------------------------------------------
if ! env "${TELEGRAF_ENV[@]}" "$TELEGRAF_BIN" --config "$RESOLVED_CONF" --test >/dev/null 2>&1; then
  echo "flake0-collect: config failed validation, output:" >&2
  env "${TELEGRAF_ENV[@]}" "$TELEGRAF_BIN" --config "$RESOLVED_CONF" --test >&2 || true
  exit 1
fi

rm -f "$FLAKE0_COLLECT_DIR/metrics.json" "$PIDFILE"
touch "$FLAKE0_COLLECT_DIR/metrics.json"

abort_startup() {
  local msg="$1"
  echo "$msg" >&2
  [ -f "$FLAKE0_COLLECT_DIR/telegraf.log" ] && cat "$FLAKE0_COLLECT_DIR/telegraf.log" >&2
  if [ -n "${LAUNCHER_PID:-}" ] && kill -0 "$LAUNCHER_PID" 2>/dev/null; then
    kill -TERM "$LAUNCHER_PID" 2>/dev/null || true
  fi
  local pid=""
  if [ -s "$PIDFILE" ]; then
    pid="$(cat "$PIDFILE" 2>/dev/null || true)"
    if [ -z "$pid" ] && [ ${#SUDO[@]} -gt 0 ]; then
      pid="$("${SUDO[@]}" cat "$PIDFILE" 2>/dev/null || true)"
    fi
  fi
  if [ -n "$pid" ] && [[ "$pid" =~ ^[0-9]+$ ]]; then
    "${SUDO[@]}" kill -KILL "$pid" 2>/dev/null || true
  fi
  rm -f "$PIDFILE" "$LAUNCHER_PIDFILE"
  exit 1
}

# --pidfile makes Telegraf record its own pid. Capturing $! instead would record
# the pid of the sudo wrapper, so liveness checks and SIGTERM would target sudo
# rather than the collector.
"${SUDO[@]}" env "${TELEGRAF_ENV[@]}" "$TELEGRAF_BIN" \
  --config "$RESOLVED_CONF" --pidfile "$PIDFILE" \
  > "$FLAKE0_COLLECT_DIR/telegraf.log" 2>&1 &
LAUNCHER_PID=$!
echo "$LAUNCHER_PID" > "$LAUNCHER_PIDFILE"

# health check: launcher alive, pidfile written, and a first flush landed
# (flush_interval 5s + up to 1s jitter, so allow generous headroom)
for _ in $(seq 1 20); do
  sleep 1
  if ! kill -0 "$LAUNCHER_PID" 2>/dev/null; then
    abort_startup "flake0-collect: telegraf died at startup, log follows:"
  fi
  [ -s "$FLAKE0_COLLECT_DIR/metrics.json" ] && [ -s "$PIDFILE" ] && break
done

if [ ! -s "$PIDFILE" ]; then
  abort_startup "flake0-collect: telegraf never wrote a pidfile, log follows:"
fi

# Telegraf creates the pidfile with mode 0640. Running under sudo that makes it
# root:root, so the unprivileged runner user cannot read it and stop.sh
# would have no pid to signal — the collector would keep running past the job.
if [ ${#SUDO[@]} -gt 0 ]; then
  "${SUDO[@]}" chmod 0644 "$PIDFILE" 2>/dev/null || true
fi

if [ ! -s "$FLAKE0_COLLECT_DIR/metrics.json" ]; then
  abort_startup "flake0-collect: no metrics after 20s, log follows:"
fi

echo "flake0-collect: running (pid $(cat "$PIDFILE")), writing $FLAKE0_COLLECT_DIR/metrics.json"

# --- juju log watcher -----------------------------------------------------------
# Follows Juju model logs and status once a controller appears in the runner's
# or root's client store, see docs/phase1-juju-logs.md. It prints and writes
# nothing until then. The step would not end while a background child held its
# stdout, hence all three redirects. A failed launch only warns, because
# telemetry must never break the job.
if ! setsid -f "$ACTION_DIR/scripts/juju-watch.sh" "$FLAKE0_COLLECT_DIR" </dev/null >/dev/null 2>&1; then
  echo "flake0-collect: warning: could not start the juju log watcher" >&2
fi
