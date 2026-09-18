#!/usr/bin/env bash
# flake0-plot — render a dashboard PNG from a flake0 collect bundle.
# Must never fail the calling job: this is diagnostic tooling chained after a
# step that may itself have failed, so any problem here degrades to "no
# dashboard produced", not a build failure. Hence no `set -e`.
set -uo pipefail

OUTDIR="${OUTDIR:-${RUNNER_TEMP:-/tmp}/flake0-plot}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
ACTION_PATH="${GITHUB_ACTION_PATH:-$(cd "$(dirname "$0")/.." && pwd)}"
BUNDLE_PATH="${BUNDLE_PATH:-}"

emit_output() {
  [ -n "${GITHUB_OUTPUT:-}" ] && echo "$1=$2" >> "$GITHUB_OUTPUT"
  return 0
}

rm -f "$OUTDIR/health.png" 2>/dev/null || true

if ! "$PYTHON_BIN" -c "import matplotlib" 2>/dev/null; then
  echo "flake0-plot: matplotlib not available, skipping render" >&2
  emit_output health-png ""
  exit 0
fi

if [ -z "$BUNDLE_PATH" ] || [ ! -e "$BUNDLE_PATH" ]; then
  echo "flake0-plot: no bundle at '$BUNDLE_PATH', skipping render" >&2
  emit_output health-png ""
  exit 0
fi

EXTRACT_DIR="$(mktemp -d "${RUNNER_TEMP:-/tmp}/flake0-plot-bundle.XXXXXX")"
if ! tar -xf "$BUNDLE_PATH" -C "$EXTRACT_DIR" 2>/dev/null; then
  echo "flake0-plot: could not extract bundle at '$BUNDLE_PATH', skipping render" >&2
  emit_output health-png ""
  exit 0
fi

METRICS_FILES=()
while IFS= read -r -d '' f; do
  METRICS_FILES+=("$f")
done < <(find "$EXTRACT_DIR" -maxdepth 1 -name 'metrics*.json' -print0)

if [ ${#METRICS_FILES[@]} -eq 0 ]; then
  echo "flake0-plot: no metrics*.json found in bundle, skipping render" >&2
  emit_output health-png ""
  exit 0
fi

RC=0
"$PYTHON_BIN" "$ACTION_PATH/scripts/plot_telemetry.py" "$OUTDIR" "${METRICS_FILES[@]}" || RC=$?
if [ "$RC" -ne 0 ]; then
  echo "flake0-plot: plot_telemetry.py exited $RC, no dashboard produced" >&2
fi

if [ -s "$OUTDIR/health.png" ]; then
  emit_output health-png "$OUTDIR/health.png"
else
  emit_output health-png ""
fi

exit 0
